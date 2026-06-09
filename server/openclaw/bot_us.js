'use strict';

const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
    makeCacheableSignalKeyStore,
    isJidBroadcast,
} = require('@whiskeysockets/baileys');
const pino   = require('pino');
const https  = require('https');
const http   = require('http');
const path   = require('path');
const fs     = require('fs');
const QRCode = require('qrcode-terminal');

const VESPER_HOST  = '127.0.0.1';
const VESPER_PORT  = 5000;
const VESPER_HTTPS = true;
const AUTH_DIR     = path.join(__dirname, 'auth_us');
const CONFIG_FILE  = path.join(__dirname, 'config_us.json');
const LOGGER       = pino({ level: 'warn' });

let config = {};
try { config = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')); } catch {}
const WHATSAPP_NUMBER = process.env.WHATSAPP_NUMBER || config.phone || '';

let botSock = null;

function storeMemory(text, category, source) {
    if (!text || text.length < 3) return;
    const body = JSON.stringify({ text, category, source });
    const lib = VESPER_HTTPS ? https : http;
    const req = lib.request({
        hostname: VESPER_HOST, port: VESPER_PORT,
        path: '/store_memory', method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
        rejectUnauthorized: false, timeout: 10000
    }, () => {});
    req.on('error', () => {});
    req.write(body); req.end();
}

process.on('uncaughtException',  e => console.error('[openclaw-us] uncaught:', e.message));
process.on('unhandledRejection', e => console.error('[openclaw-us] unhandled:', e?.message || e));

let _restarting = false;
async function startBot() {
    if (_restarting) return;
    try {
        const { version }          = await fetchLatestBaileysVersion();
        const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

        const sock = makeWASocket({
            version, logger: LOGGER,
            auth: { creds: state.creds, keys: makeCacheableSignalKeyStore(state.keys, LOGGER) },
            printQRInTerminal: false,
            generateHighQualityLinkPreview: false,
            syncFullHistory: false,
            markOnlineOnConnect: false,
            retryRequestDelayMs: 2000,
            getMessage: async () => undefined,
            shouldIgnoreJid: jid => jid === 'status@broadcast' || isJidBroadcast(jid),
        });

        botSock = sock;
        sock.ev.on('creds.update', saveCreds);

        sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
            if (qr) {
                QRCode.generate(qr, { small: true }, (code) => {
                    fs.writeFileSync('/tmp/vesper_us_qr.txt', code);
                    console.log('\n' + code);
                });
            }

            if (!sock.authState.creds.registered && WHATSAPP_NUMBER && !qr) {
                try {
                    await new Promise(r => setTimeout(r, 2000));
                    const code = await sock.requestPairingCode(WHATSAPP_NUMBER);
                    const fmt  = (code.match(/.{4}/g) || [code]).join('-');
                    fs.writeFileSync('/tmp/vesper_us_pair_code.txt', fmt);
                    console.log(`\n  US PAIRING CODE: ${fmt}\n`);
                } catch (e) { console.log(`[openclaw-us] pairing error: ${e.message}`); }
            }

            if (connection === 'close') {
                const errCode = lastDisconnect?.error?.output?.statusCode;
                console.log(`[openclaw-us] disconnected (${errCode})`);
                _restarting = false;
                if (errCode !== DisconnectReason.loggedOut) {
                    setTimeout(startBot, 5000);
                } else {
                    fs.rmSync(AUTH_DIR, { recursive: true, force: true });
                    fs.mkdirSync(AUTH_DIR, { recursive: true });
                    setTimeout(startBot, 3000);
                }
            } else if (connection === 'open') {
                _restarting = false;
                console.log(`[openclaw-us] ✅ Connected as ${sock.user?.id}`);
            }
        });

        sock.ev.on('messages.upsert', async ({ messages, type }) => {
            for (const msg of messages) {
                if (!msg.message) continue;
                if (isJidBroadcast(msg.key.remoteJid)) continue;

                const jid    = msg.key.remoteJid || '';
                const fromMe = msg.key.fromMe === true;

                if (jid === 'status@broadcast') continue;

                const msgTimeSec = Number(msg.messageTimestamp || 0);
                const nowSec     = Math.floor(Date.now() / 1000);
                if (nowSec - msgTimeSec > 300) continue;

                const text = msg.message?.conversation ||
                             msg.message?.extendedTextMessage?.text || '';
                if (!text || text.length < 2) continue;

                const from = fromMe ? 'Me (US)' : jid.split('@')[0];
                const doc  = `[WhatsApp US] ${from}: ${text}`;
                const cat  = jid.endsWith('@g.us') ? 'whatsapp_group' : 'whatsapp';
                storeMemory(doc, cat, `whatsapp_us:${jid.split('@')[0]}`);

                const arrow = fromMe ? 'ME→' + jid : jid + '→ME';
                console.log(`[openclaw-us] msg [${type}] ${arrow}: ${text.slice(0, 60)}`);
            }
        });

    } catch(e) {
        console.error('[openclaw-us] startBot error:', e.message);
        _restarting = false;
        setTimeout(startBot, 8000);
    }
}

startBot();
