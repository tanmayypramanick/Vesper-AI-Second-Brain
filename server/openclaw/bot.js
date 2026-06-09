'use strict';

const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
    makeCacheableSignalKeyStore,
    isJidBroadcast,
    downloadMediaMessage
} = require('@whiskeysockets/baileys');
const pino   = require('pino');
const https  = require('https');
const http   = require('http');
const path   = require('path');
const fs     = require('fs');
const QRCode = require('qrcode-terminal');
const { execSync, exec } = require('child_process');

// ── Config ────────────────────────────────────────────────────────────────────
const VESPER_HOST   = '127.0.0.1';
const VESPER_PORT   = 5000;
const VESPER_HTTPS  = true;
const AUTH_DIR      = path.join(__dirname, 'auth');
const CONFIG_FILE   = path.join(__dirname, 'config.json');
const HISTORY_FILE  = path.join(__dirname, 'history.json');
const SELF_JID_FILE = '/tmp/vesper_wa_self_jid.txt';
const MAC_HOST      = process.env.MAC_HOST || '10.0.0.169';
const MAC_USER      = process.env.MAC_USER || 'tanmay';
const MAC_KEY       = process.env.MAC_KEY  || '/home/tanmay/.ssh/mac_key';
const LOGGER        = pino({ level: 'warn' });

let config = {};
try { config = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')); } catch {}
const WHATSAPP_NUMBER = process.env.WHATSAPP_NUMBER || config.phone || '';
// OWNER_JID: the only JID the bot will REPLY to. Set to your US number e.g. '13xxxxxxxxxx@s.whatsapp.net'
// Empty string = bot ingests everything but replies to NO external contacts.
const OWNER_JID   = process.env.OWNER_JID   || config.owner_jid   || '';
const OWNER_LID   = process.env.OWNER_LID   || config.owner_lid   || '';
// MORNING_JID: where morning briefing is delivered. Leave empty to use OWNER_JID.
const MORNING_JID = process.env.MORNING_JID  || config.morning_jid || '';

let botSock = null;

// ── History: persist across bot restarts ──────────────────────────────────────
function loadHistory() {
    try { return JSON.parse(fs.readFileSync(HISTORY_FILE, 'utf8')); } catch { return {}; }
}
function saveHistory(h) {
    try { fs.writeFileSync(HISTORY_FILE, JSON.stringify(h)); } catch {}
}

const sessionState = { modelOverride: null, history: loadHistory() };

// ── HTTP API server (port 5001) ───────────────────────────────────────────────
const apiServer = http.createServer(async (req, res) => {
    if (req.method !== 'POST') { res.writeHead(405); res.end(); return; }
    let body = '';
    req.on('data', c => body += c);
    req.on('end', async () => {
        try {
            const { jid, message, type } = JSON.parse(body);
            if (!botSock || !jid || !message) { res.writeHead(400); res.end('not ready'); return; }
            if (type === 'audio' && fs.existsSync(message)) {
                await botSock.sendMessage(jid, { audio: fs.readFileSync(message), mimetype: 'audio/ogg; codecs=opus', ptt: true });
            } else {
                await botSock.sendMessage(jid, { text: message });
            }
            res.writeHead(200); res.end('sent');
        } catch (e) { res.writeHead(500); res.end(e.message); }
    });
});
apiServer.listen(5001, '127.0.0.1', () => console.log('[openclaw] HTTP API on :5001'));

// ── Helpers ───────────────────────────────────────────────────────────────────
function httpreq(opts, body) {
    return new Promise((resolve) => {
        const lib = VESPER_HTTPS ? https : http;
        let data = '';
        const req = lib.request({ ...opts, rejectUnauthorized: false }, (res) => {
            res.on('data', c => data += c);
            res.on('end', () => resolve(data));
        });
        req.on('error', e => resolve(`Error: ${e.message}`));
        req.on('timeout', () => { req.destroy(); resolve('timeout'); });
        if (body) req.write(body);
        req.end();
    });
}

async function _askVesperOnce(question, history, timeoutMs) {
    const body = JSON.stringify({ question, voice: 'af_heart', history: history.slice(-6), source: 'whatsapp' });
    let answer = '', buf = '';
    await new Promise((resolve) => {
        const lib = VESPER_HTTPS ? https : http;
        const req = lib.request({
            hostname: VESPER_HOST, port: VESPER_PORT,
            path: '/voice_fast', method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
            rejectUnauthorized: false, timeout: timeoutMs
        }, (res) => {
            res.on('data', (chunk) => {
                buf += chunk.toString();
                const lines = buf.split('\n');
                buf = lines.pop();
                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    const p = line.slice(6).trim();
                    if (p === 'END' || !p.startsWith('T:')) continue;
                    try { answer += Buffer.from(p.slice(2), 'base64').toString() + ' '; } catch {}
                }
            });
            res.on('end', resolve);
        });
        req.on('error', resolve);
        req.on('timeout', () => { req.destroy(); resolve(); });
        req.write(body); req.end();
    });
    return answer.trim();
}

async function askVesper(question, history = []) {
    // First attempt — 30s timeout
    const first = await _askVesperOnce(question, history, 30000);
    if (first) return first;
    // Server may be restarting — wait 10s and retry once
    console.log('[openclaw] No response from Vesper, retrying in 10s...');
    await new Promise(r => setTimeout(r, 10000));
    const second = await _askVesperOnce(question, history, 30000);
    return second || '(no response — try again)';
}

async function askVesperSmart(question, history = []) {
    // Use /ask endpoint for qwen3 — returns JSON, no SSE, no audio overhead
    // qwen3 can take 40-60s on CPU so we wait up to 90s
    const body = JSON.stringify({ question, model: 'qwen3-ask', history: history.slice(-6) });
    return new Promise((resolve) => {
        const lib = VESPER_HTTPS ? https : http;
        let data = '';
        const req = lib.request({
            hostname: VESPER_HOST, port: VESPER_PORT,
            path: '/ask', method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
            rejectUnauthorized: false, timeout: 90000
        }, (res) => {
            res.on('data', c => data += c);
            res.on('end', () => {
                try {
                    const j = JSON.parse(data);
                    resolve(j.answer || '(no response — try again)');
                } catch { resolve('(no response — try again)'); }
            });
        });
        req.on('error', () => resolve('(no response — try again)'));
        req.on('timeout', () => { req.destroy(); resolve('⏳ Smart model is thinking — ask again in 30s'); });
        req.write(body); req.end();
    });
}

function storeMemory(text, category, source) {
    if (!text || text.length < 3) return;
    const body = JSON.stringify({ text, category, source });
    httpreq({
        hostname: VESPER_HOST, port: VESPER_PORT,
        path: '/store_memory', method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) }
    }, body);
}

function storeIncoming(from, text, timestamp) {
    if (!text || text.length < 3) return;
    storeMemory(`[WhatsApp Live] ${from}: ${text}`, 'whatsapp', 'openclaw_realtime');
}

function macExec(cmd, timeoutMs = 30000) {
    return new Promise((resolve) => {
        try {
            const ssh = `ssh -i ${MAC_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=8 ${MAC_USER}@${MAC_HOST}`;
            const out = execSync(`${ssh} ${JSON.stringify(cmd)}`, { timeout: timeoutMs }).toString().trim();
            resolve(out.slice(0, 2000) || '(done)');
        } catch (e) { resolve(`Error: ${e.message.split('\n')[0].slice(0, 200)}`); }
    });
}

function macExecBg(cmd) {
    return new Promise((resolve) => {
        const ssh = `ssh -i ${MAC_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=8 ${MAC_USER}@${MAC_HOST}`;
        exec(`${ssh} ${JSON.stringify(cmd + ' > /tmp/mac_task_out.txt 2>&1 &')}`, (err) => {
            resolve(err ? `Failed: ${err.message.slice(0, 100)}` : 'Task started on Mac.');
        });
    });
}

// ── Reverse geocode lat/lon → place name (Nominatim) ─────────────────────────
async function reverseGeocode(lat, lon) {
    return new Promise((resolve) => {
        https.get({
            hostname: 'nominatim.openstreetmap.org',
            path: `/reverse?lat=${lat}&lon=${lon}&format=json&zoom=16`,
            headers: { 'User-Agent': 'VesperPersonalAI/1.0' },
            timeout: 6000
        }, (res) => {
            let d = '';
            res.on('data', c => d += c);
            res.on('end', () => {
                try {
                    const j = JSON.parse(d);
                    const a = j.address || {};
                    const parts = [
                        a.amenity || a.building || a.shop || a.office || a.leisure,
                        a.road || a.pedestrian,
                        a.suburb || a.neighbourhood || a.quarter,
                        a.city || a.town || a.village || a.municipality,
                        a.country
                    ].filter(Boolean);
                    resolve(parts.slice(0, 4).join(', ') || j.display_name?.split(',').slice(0, 3).join(',') || `${lat},${lon}`);
                } catch { resolve(`${lat},${lon}`); }
            });
        }).on('error', () => resolve(`${lat},${lon}`)).on('timeout', () => resolve(`${lat},${lon}`));
    });
}

// ── Task execution ────────────────────────────────────────────────────────────
async function handleTask(text, jid) {
    const imsgMatch = text.match(/send (?:i?message|text) to ([^:]+)[:\s]+(.+)/i);
    if (imsgMatch) {
        const [, contact, msg] = imsgMatch;
        const script = `osascript -e 'tell application "Messages" to send "${msg.replace(/"/g, '\\"')}" to buddy "${contact.trim()}"'`;
        const out = await macExec(script, 15000);
        return `iMessage sent to ${contact.trim()}: "${msg}"\n${out.includes('Error') ? '⚠️ ' + out : '✅ Done'}`;
    }
    const emailMatch = text.match(/send (?:email|mail|gmail) to ([^\s:,]+)[:\s]+(.+)/i);
    if (emailMatch) {
        const [, to, body_text] = emailMatch;
        const subjectMatch = body_text.match(/(?:subject[:\s]+)?([^-]+)\s*[-–]\s*(.+)/);
        const subject = subjectMatch ? subjectMatch[1].trim() : 'Message from Vesper';
        const body_content = subjectMatch ? subjectMatch[2].trim() : body_text.trim();
        const script = `osascript -e 'tell application "Mail" to make new outgoing message with properties {subject:"${subject}", content:"${body_content}", visible:false}' -e 'tell result to make new to recipient with properties {address:"${to}"}' -e 'tell result to send'`;
        const out = await macExec(script, 20000);
        return `Email sent to ${to}\nSubject: ${subject}\n${out.includes('Error') ? '⚠️ ' + out : '✅ Done'}`;
    }
    return null;
}

// ── Process incoming messages ─────────────────────────────────────────────────
async function handleMessage(msg, sock) {
    if (!msg.message) return;
    if (isJidBroadcast(msg.key.remoteJid)) return;
    if (BOT_SENT_IDS.has(msg.key.id)) { BOT_SENT_IDS.delete(msg.key.id); return; }

    const jid     = msg.key.remoteJid || '';
    const fromMe  = msg.key.fromMe === true;

    if (jid.endsWith('@g.us') || jid === 'status@broadcast') return;

    const msgTimeSec = Number(msg.messageTimestamp || 0);
    const nowSec     = Math.floor(Date.now() / 1000);
    if (nowSec - msgTimeSec > 120) return;

    // ── Store ALL incoming messages ────────────────────────────────────────
    if (!fromMe) {
        const txt = msg.message?.conversation || msg.message?.extendedTextMessage?.text || '';
        if (txt) storeIncoming(jid.split('@')[0], txt, msg.messageTimestamp);
    }

    if (fromMe) {
        // Save Tanmay's outgoing messages too — essential for full conversation context
        const outTxt = msg.message?.conversation || msg.message?.extendedTextMessage?.text || '';
        const contact = jid.split('@')[0];
        if (outTxt) storeMemory('[WhatsApp Sent] To ' + contact + ': ' + outTxt, 'whatsapp', 'openclaw_outgoing');
        return;
    }

    // ── Owner gate — ONLY reply to designated owner JID ──────────────────
    // If owner not configured OR message is not from owner → ingest only.
    // Allow if message matches owner by JID or by WhatsApp LID (multi-device)
    const _isOwner = (OWNER_JID && jid === OWNER_JID) || (OWNER_LID && jid === OWNER_LID);
    if (!_isOwner) {
        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        return;
    }

    try { await sock.sendPresenceUpdate('composing', jid); } catch {}

    // ── Location / Live Location ───────────────────────────────────────────
    const locMsg = msg.message?.locationMessage || msg.message?.liveLocationMessage;
    if (locMsg) {
        const lat  = (locMsg.degreesLatitude  || 0).toFixed(6);
        const lon  = (locMsg.degreesLongitude || 0).toFixed(6);
        const isLive = !!msg.message?.liveLocationMessage;
        // WhatsApp often includes the place name already
        let place = (locMsg.name || locMsg.address || '').trim();
        if (!place) place = await reverseGeocode(lat, lon);

        const now = new Date().toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' });
        const doc = `[Location${isLive ? ' Live' : ''}] Tanmay is at ${place} (${lat},${lon}) at ${now}`;
        storeMemory(doc, 'location', 'whatsapp_location');

        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        await trackSend(sock, jid, { text: `📍 Saved: ${place}${isLive ? '\n🔴 Live updates will keep saving' : ''}` });
        return;
    }

    // ── Image / Photo — save caption & note ───────────────────────────────
    const imgMsg = msg.message?.imageMessage;
    if (imgMsg) {
        const caption = (imgMsg.caption || '').trim();
        const now = new Date().toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' });
        const doc = caption
            ? `[Photo] Tanmay shared a photo on ${now}: "${caption}"`
            : `[Photo] Tanmay shared a photo on ${now}`;
        storeMemory(doc, 'media', 'whatsapp_photo');
        if (caption) {
            // Treat caption as a question/note to Vesper
            const chatHistory = sessionState.history[jid] || [];
            const answer = await askVesper(caption, chatHistory);
            chatHistory.push({ role: 'user', content: caption });
            chatHistory.push({ role: 'assistant', content: answer });
            if (chatHistory.length > 12) chatHistory.splice(0, 2);
            sessionState.history[jid] = chatHistory;
            saveHistory(sessionState.history);
            try { await sock.sendPresenceUpdate('available', jid); } catch {}
            await trackSend(sock, jid, { text: answer });
        } else {
            try { await sock.sendPresenceUpdate('available', jid); } catch {}
            await trackSend(sock, jid, { text: '📸 Photo saved to memory.' });
        }
        return;
    }

    // ── Voice note (PTT) ──────────────────────────────────────────────────
    const audioMsg = msg.message?.audioMessage;
    if (audioMsg) {
        try {
            const buf = await downloadMediaMessage(msg, 'buffer', {});
            const tmpOgg = `/tmp/wa_voice_${Date.now()}.ogg`;
            const tmpWav = tmpOgg.replace('.ogg', '.wav');
            fs.writeFileSync(tmpOgg, buf);
            let audioPath = tmpOgg;
            // Try ffmpeg; if unavailable, pass ogg directly (faster-whisper can handle it)
            try { execSync(`ffmpeg -y -i ${tmpOgg} -ar 16000 -ac 1 ${tmpWav} 2>/dev/null`, { timeout: 15000 }); audioPath = tmpWav; } catch {}

            // Send audio file to server transcribe endpoint
            const wavBuf = fs.readFileSync(audioPath);
            const transcript = await new Promise((resolve) => {
                const lib = VESPER_HTTPS ? https : http;
                const req = lib.request({
                    hostname: VESPER_HOST, port: VESPER_PORT,
                    path: '/transcribe', method: 'POST',
                    headers: { 'Content-Type': 'audio/ogg', 'Content-Length': wavBuf.length },
                    rejectUnauthorized: false, timeout: 30000
                }, (res) => {
                    let d = '';
                    res.on('data', c => d += c);
                    res.on('end', () => { try { resolve(JSON.parse(d).text || ''); } catch { resolve(''); } });
                });
                req.on('error', () => resolve(''));
                req.write(wavBuf); req.end();
            });
            try { fs.unlinkSync(tmpOgg); } catch {}
            try { fs.unlinkSync(tmpWav); } catch {}

            if (!transcript) {
                try { await sock.sendPresenceUpdate('available', jid); } catch {}
                await trackSend(sock, jid, { text: "Couldn't transcribe. Try speaking clearly." });
                return;
            }

            // Store transcript as memory
            storeMemory(`[Voice Note] Tanmay said: "${transcript}"`, 'voice', 'whatsapp_voice');

            // Ask Vesper with transcript
            const chatHistory = sessionState.history[jid] || [];
            const answer = await askVesper(transcript, chatHistory);
            chatHistory.push({ role: 'user', content: transcript });
            chatHistory.push({ role: 'assistant', content: answer });
            if (chatHistory.length > 12) chatHistory.splice(0, 2);
            sessionState.history[jid] = chatHistory;
            saveHistory(sessionState.history);

            try { await sock.sendPresenceUpdate('available', jid); } catch {}
            await trackSend(sock, jid, { text: `🗣️ _"${transcript.slice(0, 120)}"_\n\n${answer}` });
        } catch (e) {
            try { await sock.sendPresenceUpdate('available', jid); } catch {}
            await trackSend(sock, jid, { text: `Voice error: ${e.message.slice(0, 100)}` });
        }
        return;
    }

    // ── Text message ──────────────────────────────────────────────────────
    const text = (
        msg.message?.conversation ||
        msg.message?.extendedTextMessage?.text || ''
    ).trim();

    if (!text) { try { await sock.sendPresenceUpdate('available', jid); } catch {}; return; }

    console.log(`[openclaw] ← ${text.slice(0, 100)}`);

    // Natural language memory: "remember that X" or "note that X" (no / required)
    const natRemember = text.match(/^(?:hey vesper[,!]?\s*)?(?:please\s+)?(?:remember|note|save)\s+(?:that\s+)?(.+)/i);
    if (natRemember && !text.startsWith('/')) {
        const fact = natRemember[1].trim();
        storeMemory(`[Manual Note] ${fact}`, 'manual', 'whatsapp_user');
        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        await trackSend(sock, jid, { text: `✅ Got it — remembered: "${fact.slice(0, 100)}"` });
        return;
    }

    if (!sessionState.history[jid]) sessionState.history[jid] = [];
    const chatHistory = sessionState.history[jid];

    // ── Commands ───────────────────────────────────────────────────────────
    if (text.startsWith('/model')) {
        const m = (text.split(/\s+/)[1] || '').toLowerCase();
        if (['dolphin','phi'].includes(m))          { sessionState.modelOverride = 'dolphin'; await trackSend(sock, jid, { text: '🐬 Dolphin — fast, uncensored, personal' }); }
        else if (['qwen3','qwen','smart'].includes(m)) { sessionState.modelOverride = 'qwen3';   await trackSend(sock, jid, { text: '🧠 Qwen3 — smarter, analytical (~10s)' }); }
        else if (['auto',''].includes(m))            { sessionState.modelOverride = null;       await trackSend(sock, jid, { text: '🔄 Auto routing restored' }); }
        else                                          { await trackSend(sock, jid, { text: '/model dolphin | /model qwen3 | /model auto' }); }
        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        return;
    }

    if (text === '/reset') {
        sessionState.history[jid] = [];
        sessionState.modelOverride = null;
        saveHistory(sessionState.history);
        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        await trackSend(sock, jid, { text: '🔄 Conversation reset.' });
        return;
    }

    // /remember <fact> — manually store any fact
    if (/^\/(remember|note|save)\s+/i.test(text)) {
        const fact = text.replace(/^\/(remember|note|save)\s+/i, '').trim();
        storeMemory(`[Manual Note] ${fact}`, 'manual', 'whatsapp_user');
        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        await trackSend(sock, jid, { text: `✅ Remembered: "${fact.slice(0, 120)}"` });
        return;
    }

    // /forget <query> — tell Vesper to stop tracking something (just a note)
    if (/^\/(forget|delete)\s+/i.test(text)) {
        const what = text.replace(/^\/(forget|delete)\s+/i, '').trim();
        storeMemory(`[Forget Request] Tanmay wants to forget: ${what}`, 'manual', 'whatsapp_user');
        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        await trackSend(sock, jid, { text: `🗑️ Noted — will de-prioritize: "${what.slice(0, 80)}"` });
        return;
    }

    if (text === '/settings' || text === '!help' || text === '/help') {
        const curModel = sessionState.modelOverride || 'auto';
        const histLen  = Math.floor((chatHistory.length || 0) / 2);
        const helpText = `⚙️ *Vesper*\n\n*Model:* ${curModel} | *History:* ${histLen} turns\n\n` +
            `*Model:*\n  /model dolphin — fast, personal\n  /model qwen3 — smart, analytical\n  /model auto — auto-route\n\n` +
            `*Memory:*\n  /remember <fact> — store anything\n  /forget <thing> — de-prioritize\n  /reset — clear history\n\n` +
            `*Location:* share via Attach → Location\n\n` +
            `*Send:*\n  send message to [name]: [text]\n  send email to [x@y.com]: subject - body\n\n` +
            `*Mac:*\n  !mac [cmd]  |  !claude [task]  |  !claude-out\n\n` +
            `*Debug:*  !status`;
        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        await trackSend(sock, jid, { text: helpText });
        return;
    }

    if (text === '!status') {
        const vesper = await httpreq({ hostname: VESPER_HOST, port: VESPER_PORT, path: '/health', rejectUnauthorized: false });
        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        await trackSend(sock, jid, { text: `Vesper: ${vesper}\nBot: ✅ connected\nHistory: ${Math.floor(chatHistory.length/2)} turns (saved)` });
        return;
    }

    if (text.startsWith('!mac ')) {
        await trackSend(sock, jid, { text: '🔧 Running...' });
        const out = await macExec(text.slice(5));
        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        await trackSend(sock, jid, { text: `Mac:\n${out}` });
        return;
    }

    if (text.startsWith('!claude ')) {
        const task = text.slice(8);
        await trackSend(sock, jid, { text: '🤖 Starting Claude on Mac...' });
        const status = await macExecBg(`/usr/local/bin/claude -p '${task.replace(/'/g, "\\'")}'`);
        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        await trackSend(sock, jid, { text: status + '\nSend !claude-out to check.' });
        return;
    }

    if (text === '!claude-out') {
        const out = await macExec('cat /tmp/mac_task_out.txt 2>/dev/null | tail -80');
        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        await trackSend(sock, jid, { text: out || 'No output yet.' });
        return;
    }

    const taskResult = await handleTask(text, jid);
    if (taskResult) {
        try { await sock.sendPresenceUpdate('available', jid); } catch {}
        await trackSend(sock, jid, { text: taskResult });
        return;
    }

    // ── Ask Vesper ─────────────────────────────────────────────────────────
    let answer;
    if (sessionState.modelOverride === 'qwen3') {
        // qwen3 uses /ask endpoint (JSON, 90s timeout) — SSE would time out on CPU inference
        answer = await askVesperSmart(text, chatHistory);
    } else {
        let queryText = text;
        if (sessionState.modelOverride === 'dolphin') queryText = `[force:dolphin] ${text}`;
        answer = await askVesper(queryText, chatHistory);
    }

    chatHistory.push({ role: 'user', content: text });
    chatHistory.push({ role: 'assistant', content: answer });
    if (chatHistory.length > 12) chatHistory.splice(0, 2);
    saveHistory(sessionState.history);

    try { await sock.sendPresenceUpdate('available', jid); } catch {}
    await trackSend(sock, jid, { text: answer });
}

// ── Bot startup ───────────────────────────────────────────────────────────────
process.on('uncaughtException',  e => console.error('[openclaw] uncaught:', e.message));
process.on('unhandledRejection', e => console.error('[openclaw] unhandled:', e?.message || e));

const BOT_SENT_IDS = new Set();
function trackSend(sock, jid, content) {
    return sock.sendMessage(jid, content).then(sent => {
        if (sent?.key?.id) BOT_SENT_IDS.add(sent.key.id);
        return sent;
    }).catch(e => console.error('[openclaw] send error:', e.message));
}

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
                    fs.writeFileSync('/tmp/vesper_qr.txt', code);
                    console.log('\n' + code);
                });
            }

            if (!sock.authState.creds.registered && WHATSAPP_NUMBER && !qr) {
                try {
                    await new Promise(r => setTimeout(r, 2000));
                    const code = await sock.requestPairingCode(WHATSAPP_NUMBER);
                    const fmt  = (code.match(/.{4}/g) || [code]).join('-');
                    fs.writeFileSync('/tmp/vesper_pair_code.txt', fmt);
                    console.log(`\n════════════════════════════════════════`);
                    console.log(`  WHATSAPP PAIRING CODE: ${fmt}`);
                    console.log(`════════════════════════════════════════`);
                } catch (e) { console.log(`[openclaw] pairing code error: ${e.message}`); }
            }

            if (connection === 'close') {
                const errCode = lastDisconnect?.error?.output?.statusCode;
                console.log(`[openclaw] disconnected (${errCode})`);
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
                const self = sock.user?.id;
                console.log(`[openclaw] ✅ Connected as ${self}`);
                if (self) {
                    const selfJid = self.replace(/:\d+@/, '@');
                    fs.writeFileSync(SELF_JID_FILE, selfJid);
                    console.log(`[openclaw] Ready. Self JID: ${selfJid}`);
                }
            }
        });

        sock.ev.on('messages.upsert', async ({ messages, type }) => {
            for (const msg of messages) {
                if (!msg.message) continue;
                const jid  = msg.key.remoteJid || '';
                const from = msg.key.fromMe ? 'ME→' + jid : jid + '→ME';
                const text = msg.message?.conversation || msg.message?.extendedTextMessage?.text || '[media]';
                console.log(`[openclaw] msg [${type}] ${from}: ${text.slice(0, 60)}`);
                handleMessage(msg, sock).catch(e => console.error('[openclaw] msg error:', e.message));
            }
        });

    } catch(e) {
        console.error('[openclaw] startBot error:', e.message);
        _restarting = false;
        setTimeout(startBot, 8000);
    }
}

startBot();
