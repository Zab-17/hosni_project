const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, makeCacheableSignalKeyStore, fetchLatestBaileysVersion } = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');
const express = require('express');
const QRCode = require('qrcode');
const pino = require('pino');
const NodeCache = require('node-cache');
const fs = require('fs');
const path = require('path');

const app = express();
app.use(express.json());

const PORT = process.env.BRIDGE_PORT || 3001;
const PYTHON_WEBHOOK_URL = process.env.PYTHON_WEBHOOK_URL || 'http://localhost:8000/webhook/whatsapp';
const AUTH_DIR = process.env.WA_SESSION_PATH || './auth_session';

const logger = pino({ level: 'warn' });

let sock = null;
let qrCode = null;
let isConnected = false;
let reconnectAttempts = 0;
const MAX_RECONNECT_DELAY = 60000; // cap at 60s
let lastMessageSentAt = 0;       // timestamp of last successful send
let lastMessageReceivedAt = 0;   // timestamp of last received message
let sendFailCount = 0;           // consecutive send failures
let isReconnecting = false;      // prevent concurrent reconnection attempts

// Message retry counter cache (survives socket reconnects)
const msgRetryCounterCache = new NodeCache({ stdTTL: 600, checkperiod: 60 });

// In-memory message store for retry/re-encryption handling
const messageStore = {};
function storeMessage(msgId, message) {
    if (msgId && message) {
        messageStore[msgId] = message;
        setTimeout(() => { delete messageStore[msgId]; }, 10 * 60 * 1000);
    }
}

let livenessInterval = null;
let disconnectedSince = Date.now();   // updated to null when connected
const DISCONNECT_EXIT_AFTER_MS = 10 * 60 * 1000;  // 10 min stuck → exit so start.sh restarts us

// Watchdog: if not connected for >10 min, exit the process so start.sh's
// while-true loop restarts the bridge cleanly. Catches any stuck state the
// in-process reconnect logic can't recover from.
setInterval(() => {
    if (isConnected) return;
    if (disconnectedSince && Date.now() - disconnectedSince > DISCONNECT_EXIT_AFTER_MS) {
        console.error(`WATCHDOG: bridge disconnected for >${DISCONNECT_EXIT_AFTER_MS / 60000}min. Exiting for clean restart.`);
        process.exit(1);
    }
}, 60 * 1000);

function wipeAuthDir() {
    try {
        for (const f of fs.readdirSync(AUTH_DIR)) {
            try { fs.rmSync(path.join(AUTH_DIR, f), { force: true }); } catch (_) {}
        }
        console.log('Auth dir wiped — fresh QR will be generated.');
    } catch (e) {
        console.error('Failed to wipe auth dir:', e.message);
    }
}

function startLivenessCheck() {
    if (livenessInterval) clearInterval(livenessInterval);

    livenessInterval = setInterval(async () => {
        if (!isConnected || !sock) return;

        try {
            // Try to query our own status — if socket is dead this will throw
            await Promise.race([
                sock.fetchStatus(sock.user?.id || '0@s.whatsapp.net').catch(() => null),
                new Promise((_, reject) => setTimeout(() => reject(new Error('Liveness timeout')), 10000)),
            ]);
            console.log('Liveness check: OK');
        } catch (err) {
            console.error(`LIVENESS FAILED: ${err.message} — socket may be zombie`);
            sendFailCount++;

            if (sendFailCount >= 2) {
                console.error('ZOMBIE DETECTED via liveness check. Force reconnecting...');
                isConnected = false;
                if (livenessInterval) clearInterval(livenessInterval);
                connectToWhatsApp();
            }
        }
    }, 2 * 60 * 1000); // Check every 2 minutes
}

async function connectToWhatsApp() {
    if (isReconnecting) {
        console.log('Connection attempt already in progress, skipping duplicate');
        return;
    }
    isReconnecting = true;

    // Close existing socket cleanly before creating a new one
    if (sock) {
        try { sock.ev.removeAllListeners(); sock.end(undefined); } catch (_) {}
        sock = null;
    }

    try {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion();
    console.log('Using WA version:', version);

    sock = makeWASocket({
        version,
        auth: {
            creds: state.creds,
            keys: makeCacheableSignalKeyStore(state.keys, logger),
        },
        logger,
        syncFullHistory: false,
        markOnlineOnConnect: true,
        keepAliveIntervalMs: 30000,
        retryRequestDelayMs: 2000,
        msgRetryCounterCache,
        generateHighQualityLinkPreview: false,
        getMessage: async (key) => {
            const msg = messageStore[key.id];
            if (msg) return msg;
            return { conversation: '' };
        },
    });

    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            qrCode = qr;
            console.log('QR code received. Visit /qr to scan.');
        }

        if (connection === 'close') {
            isConnected = false;
            isReconnecting = false; // release lock so reconnect can proceed
            disconnectedSince = disconnectedSince || Date.now();
            const statusCode = (lastDisconnect?.error)?.output?.statusCode;
            const loggedOut = statusCode === DisconnectReason.loggedOut;
            console.log('Connection closed. Status:', statusCode, '| LoggedOut:', loggedOut);

            if (loggedOut) {
                // Saved creds are now invalid. Without wiping them, every reconnect
                // loops back to loggedOut and the bridge sits dead waiting for a human.
                console.error('CRITICAL: WhatsApp session logged out. Wiping creds for fresh QR.');
                wipeAuthDir();
                reconnectAttempts = 0;
                qrCode = null;
                setTimeout(connectToWhatsApp, 2000);
            } else {
                reconnectAttempts++;
                // Exponential backoff: 5s, 10s, 20s, 40s, capped at 60s
                const delay = Math.min(5000 * Math.pow(2, reconnectAttempts - 1), MAX_RECONNECT_DELAY);
                console.log(`Reconnecting in ${delay / 1000}s (attempt ${reconnectAttempts})...`);
                setTimeout(connectToWhatsApp, delay);
            }
        } else if (connection === 'open') {
            isConnected = true;
            isReconnecting = false; // release lock on successful connection
            reconnectAttempts = 0;
            qrCode = null;
            disconnectedSince = null;
            console.log('WhatsApp connected!');
            startLivenessCheck();
        }
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return;

        for (const msg of messages) {
            // Store all messages for retry/re-encryption
            if (msg.key?.id && msg.message) {
                storeMessage(msg.key.id, msg.message);
            }
            lastMessageReceivedAt = Date.now();
            if (msg.key.fromMe) continue;

            // Ignore group + broadcast traffic — replying to those JIDs causes
            // xml-not-well-formed stream errors that crash the connection.
            const remoteJid = msg.key.remoteJid || '';
            if (remoteJid.endsWith('@g.us') || remoteJid.endsWith('@broadcast') || remoteJid.endsWith('@newsletter')) continue;

            const text = msg.message?.conversation
                || msg.message?.extendedTextMessage?.text
                || '';
            if (!text) continue;

            // Prefer senderPn (real phone number) over LID
            let from;
            if (msg.key.senderPn) {
                // senderPn gives us the real phone number (e.g. 201101588288@s.whatsapp.net)
                from = msg.key.senderPn.replace('@s.whatsapp.net', '');
            } else {
                from = remoteJid.replace('@s.whatsapp.net', '').replace(/:.*@lid$/, '');
            }
            console.log(`Message from ${from} (jid: ${remoteJid}): ${text}`);

            try {
                await fetch(PYTHON_WEBHOOK_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ from, text }),
                });
            } catch (err) {
                console.error('Failed to forward to Python:', err.message);
            }
        }
    });
    } catch (err) {
        console.error('connectToWhatsApp error:', err.message);
        isReconnecting = false;
        reconnectAttempts++;
        const delay = Math.min(5000 * Math.pow(2, reconnectAttempts - 1), MAX_RECONNECT_DELAY);
        console.log(`Retrying connection in ${delay / 1000}s...`);
        setTimeout(connectToWhatsApp, delay);
    }
}

// REST API

app.get('/health', (req, res) => {
    const now = Date.now();
    const sinceSend = lastMessageSentAt ? Math.round((now - lastMessageSentAt) / 1000) : null;
    const sinceRecv = lastMessageReceivedAt ? Math.round((now - lastMessageReceivedAt) / 1000) : null;
    const socketAlive = !!(sock && sock.ws?.readyState !== undefined ? sock.ws.readyState === 1 : isConnected);
    const healthy = isConnected && sendFailCount < 3;

    res.json({
        status: healthy ? 'ok' : 'degraded',
        connected: isConnected,
        socketAlive,
        sendFailCount,
        lastSendSecondsAgo: sinceSend,
        lastRecvSecondsAgo: sinceRecv,
    });
});

app.get('/qr', async (req, res) => {
    if (isConnected) return res.send('<h1>Already connected!</h1>');
    if (!qrCode) return res.send('<h1>Generating QR code... Refresh in a few seconds.</h1>');

    const qrImage = await QRCode.toDataURL(qrCode);
    res.send(`
        <html><body style="display:flex;justify-content:center;align-items:center;height:100vh;flex-direction:column;background:#111;color:#fff;font-family:sans-serif">
            <h2>Scan with WhatsApp</h2>
            <img src="${qrImage}" style="width:300px;height:300px;border-radius:12px"/>
            <p style="color:#888;margin-top:16px">WhatsApp > Linked Devices > Link a Device</p>
            <script>setTimeout(() => location.reload(), 15000)</script>
        </body></html>
    `);
});

app.post('/send', async (req, res) => {
    if (!isConnected || !sock) return res.status(503).json({ error: 'WhatsApp not connected' });

    const { to, message } = req.body;
    if (!to || !message) return res.status(400).json({ error: 'Missing to or message' });

    // Convert phone number to JID format
    // Strip any existing suffix first
    const phone = to.replace('@s.whatsapp.net', '').replace('@c.us', '').replace('@lid', '');
    const jid = `${phone}@s.whatsapp.net`;

    try {
        // Verify socket is actually alive before sending
        if (sock.ws && sock.ws.readyState !== undefined && sock.ws.readyState !== 1) {
            sendFailCount++;
            console.error(`Send failed: WebSocket not open (readyState=${sock.ws.readyState}), fail count: ${sendFailCount}`);
            return res.status(503).json({ error: 'WhatsApp WebSocket is dead (zombie connection)', zombie: true });
        }

        const result = await sock.sendMessage(jid, { text: message });
        // Store for retry/re-encryption handling
        if (result?.key?.id) {
            storeMessage(result.key.id, { conversation: message });
        }
        lastMessageSentAt = Date.now();
        sendFailCount = 0; // reset on success
        res.json({ success: true, id: result?.key?.id || 'sent' });
    } catch (err) {
        sendFailCount++;
        console.error(`Send error (fail #${sendFailCount}): ${err.message}`);

        // If 3+ consecutive failures, flag as zombie
        if (sendFailCount >= 3) {
            console.error('ZOMBIE DETECTED: 3+ consecutive send failures. Attempting reconnect...');
            isConnected = false;
            connectToWhatsApp();
        }

        if (!isConnected) {
            return res.status(503).json({ error: 'WhatsApp disconnected during send', zombie: sendFailCount >= 3 });
        }
        res.status(500).json({ error: err.message, sendFailCount });
    }
});

// Endpoint to check if a number is on WhatsApp
app.post('/check-number', async (req, res) => {
    if (!isConnected || !sock) return res.status(503).json({ error: 'WhatsApp not connected' });

    const { phone } = req.body;
    if (!phone) return res.status(400).json({ error: 'Missing phone' });

    try {
        const [result] = await sock.onWhatsApp(phone);
        res.json({ exists: result?.exists || false, jid: result?.jid || null });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

app.listen(PORT, () => {
    console.log(`WhatsApp bridge (Baileys) running on port ${PORT}`);
    console.log(`Auth dir: ${AUTH_DIR}`);
    connectToWhatsApp().catch(err => {
        console.error('Failed to connect:', err.message);
        console.log('Retrying in 10s...');
        setTimeout(() => connectToWhatsApp().catch(e => console.error('Retry failed:', e.message)), 10000);
    });
});
