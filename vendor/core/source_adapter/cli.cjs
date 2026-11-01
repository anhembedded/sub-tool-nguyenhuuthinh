#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-only
'use strict';
const [runtimeMajor, runtimeMinor] = process.versions.node.split('.').map(Number);
if (runtimeMajor < 22 || (runtimeMajor === 22 && runtimeMinor < 12)) {
  process.stderr.write('Node.js >=22.12 is required; select a current Node runtime.\n');
  process.exit(2);
}
const fs = require('node:fs');
const path = require('node:path');
const { Transform } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const { failure, runTool, validateFile } = require('./media.cjs');
const { meetsDownloadQuality } = require('./native/download-quality');
const { assertOutputRoot, acquireLock, releaseLock } = require('./safety.cjs');

const HELP = `Hongguo SSOT source adapter — exact 1920x1080 HEVC, 30fps

Offline validation (no network, no media modification):
  node cli.cjs validate --source FILE [--report JSON] [--ffmpeg FILE] [--ffprobe FILE]

Download into an isolated directory:
  node cli.cjs download --series-id ID --episodes 1-184 --output-root DIR
  node cli.cjs download --config config.example.json

Config JSON accepts seriesId, episodes (array), outputRoot, concurrency (1-4),
maxBytesPerEpisode, ffmpeg, ffprobe. CLI paths resolve from the current directory;
paths in config resolve from the config directory. Existing outputs are reused
only with a matching episode identity sidecar and full media/hash validation.
There is no 480p, bytevc2, public-web media, transcoding, or quality fallback.
`;

function parseEpisodes(value) {
  if (Array.isArray(value)) {
    if (!value.length || value.some(number => !Number.isSafeInteger(number) || number < 1 || number > 9999)) {
      throw failure('INVALID_EPISODES', 'episodes must contain integers from 1 to 9999');
    }
    return [...new Set(value)].sort((a, b) => a - b);
  }
  const numbers = [];
  for (const token of String(value || '').split(',')) {
    const match = /^(\d+)(?:-(\d+))?$/.exec(token.trim());
    if (!match) throw failure('INVALID_EPISODES', 'Use an episode list such as 1-10,12');
    const first = Number(match[1]), last = Number(match[2] || match[1]);
    if (last < first || first < 1 || last > 9999) throw failure('INVALID_EPISODES', 'Episode range is invalid');
    for (let number = first; number <= last; number++) numbers.push(number);
  }
  return parseEpisodes(numbers);
}

function parseArguments(argv) {
  if (!argv.length || argv.includes('--help') || argv.includes('-h')) return { command: 'help' };
  const command = argv[0];
  if (!['download', 'validate'].includes(command)) throw failure('INVALID_COMMAND', 'Choose download or validate; use --help');
  const values = {};
  const allowed = new Set(['config', 'series-id', 'episodes', 'output-root', 'concurrency', 'source', 'report', 'ffmpeg', 'ffprobe']);
  for (let index = 1; index < argv.length; index += 2) {
    const flag = argv[index];
    if (!flag?.startsWith('--') || !allowed.has(flag.slice(2)) || index + 1 >= argv.length || argv[index + 1].startsWith('--')) {
      throw failure('INVALID_ARGUMENT', 'Each supported option requires one value; use --help');
    }
    if (Object.hasOwn(values, flag.slice(2))) throw failure('DUPLICATE_ARGUMENT', 'Options must not be repeated');
    values[flag.slice(2)] = argv[index + 1];
  }
  let config = {};
  if (values.config) {
    const configPath = path.resolve(values.config), base = path.dirname(configPath);
    config = JSON.parse(fs.readFileSync(configPath, 'utf8').replace(/^\uFEFF/, ''));
    for (const key of ['outputRoot', 'source', 'report', 'ffmpeg', 'ffprobe']) {
      if (config[key] && (['outputRoot', 'source', 'report'].includes(key) || /[\\/]/.test(config[key]))) config[key] = path.resolve(base, config[key]);
    }
  }
  const aliases = { 'series-id': 'seriesId', 'output-root': 'outputRoot' };
  for (const [key, value] of Object.entries(values)) if (key !== 'config') config[aliases[key] || key] = value;
  if (command === 'validate') {
    if (!config.source) throw failure('MISSING_SOURCE', 'validate requires --source');
    if (config.report && path.resolve(config.report).toLowerCase() === path.resolve(config.source).toLowerCase()) {
      throw failure('REPORT_SOURCE_COLLISION', 'The validation report must not overwrite source media');
    }
    return { command, ...config, source: path.resolve(config.source), ...(config.report ? { report: path.resolve(config.report) } : {}) };
  }
  if (!/^\d{1,30}$/.test(String(config.seriesId || ''))) throw failure('INVALID_SERIES_ID', 'download requires a numeric series ID');
  if (!config.outputRoot) throw failure('MISSING_OUTPUT_ROOT', 'download requires an explicit isolated output root');
  config.episodes = parseEpisodes(config.episodes);
  config.concurrency = Number(config.concurrency ?? 2);
  config.maxBytesPerEpisode = Number(config.maxBytesPerEpisode ?? 350 * 1024 * 1024);
  if (!Number.isInteger(config.concurrency) || config.concurrency < 1 || config.concurrency > 4) throw failure('INVALID_CONCURRENCY', 'concurrency must be an integer from 1 to 4');
  if (!Number.isSafeInteger(config.maxBytesPerEpisode) || config.maxBytesPerEpisode < 1) throw failure('INVALID_SIZE_LIMIT', 'maxBytesPerEpisode must be a positive integer');
  return { command, ...config, seriesId: String(config.seriesId), outputRoot: assertOutputRoot(config.outputRoot) };
}

function writeJson(file, payload) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temp = file + '.tmp';
  fs.writeFileSync(temp, JSON.stringify(payload, null, 2) + '\n', { encoding: 'utf8' });
  fs.renameSync(temp, file);
}

function safeError(error, stage) {
  const code = typeof error.code === 'string' && /^[A-Z0-9_]+$/.test(error.code) ? error.code : 'ERROR';
  return { stage, code, status: Number(error.status || error.response?.status) || null,
    message: error.safeMessage ? error.message : `${stage} did not provide a usable permitted source or result` };
}

function isServiceRejection(error) {
  return [401, 403, 429].includes(Number(error.status || error.response?.status))
    || ['BUSINESS_REJECTED', 'SECURITY_BLOCKED'].includes(error.code);
}

async function download(config) {
  assertOutputRoot(config.outputRoot);
  // Lazy loading makes help and local validation independent from HTTP modules.
  const axios = require('axios');
  const { fetchEpisodeListFromWeb } = require('./native/episode-metadata');
  const { fetchAppPlayUrl } = require('./native/app-source');
  const { webMediaRequestOptions } = require('./native/web-media-guard');
  fs.mkdirSync(config.outputRoot, { recursive: true });
  const sources = path.join(config.outputRoot, 'sources');
  fs.mkdirSync(sources, { recursive: true });
  const sourceRelative = path.relative(fs.realpathSync(config.outputRoot), fs.realpathSync(sources));
  if (sourceRelative === '..' || sourceRelative.startsWith('..' + path.sep) || path.isAbsolute(sourceRelative)) {
    throw failure('UNSAFE_PATH', 'Sources directory escapes this isolated run through a symlink/junction');
  }
  assertOutputRoot(sources);
  const reportPath = path.join(config.outputRoot, 'download_report.json');
  const lockPath = path.join(config.outputRoot, '.download.lock');
  const lock = acquireLock(lockPath);
  const report = { schema: 2, seriesId: config.seriesId, requestedEpisodes: config.episodes,
    requestedQuality: '1080p', requiredDimensions: [1920, 1080], requiredCodec: 'hevc', mediaSource: 'app',
    publicWebMediaFallback: false, metadataSource: 'website_episode_ids_only', concurrency: config.concurrency,
    startedAt: new Date().toISOString(), status: 'starting', network: [], episodes: [],
    securityNote: 'No content keys, authorization tokens, signatures, API bodies or signed CDN URLs are saved.' };
  const save = () => { report.updatedAt = new Date().toISOString(); writeJson(reportPath, report); };
  const log = (event, payload) => process.stdout.write(JSON.stringify({ event, ...payload }) + '\n');
  let serviceRejected = false;
  const interceptor = axios.interceptors.response.use(response => {
    try {
      const url = new URL(response.config.url);
      report.network.push({ host: url.hostname, method: response.config.method, status: response.status,
        contentType: response.headers['content-type'] || null });
    } catch (_) { /* Telemetry never records request data. */ }
    return response;
  });
  const ownedTemporary = file => {
    if (path.dirname(path.resolve(file)) !== sources || !/^ep\d{2,4}_app_1080p\.(enc\.part|decoded\.part\.mp4)$/.test(path.basename(file))) {
      throw failure('UNSAFE_PATH', 'Temporary file is outside this adapter sources directory');
    }
    return file;
  };
  const removeTemporary = file => { const checked = ownedTemporary(file); if (fs.existsSync(checked)) fs.unlinkSync(checked); };
  async function downloadOne(row) {
    const final = path.join(sources, `ep${String(row.episode).padStart(2, '0')}_app_1080p.mp4`);
    const encrypted = final.replace(/\.mp4$/, '.enc.part'), decoded = final.replace(/\.mp4$/, '.decoded.part.mp4'), sidecar = final + '.source.json';
    let stage = 'resume_validation';
    row.path = final;
    try {
      if (fs.existsSync(final)) {
        if (!fs.existsSync(sidecar)) throw failure('UNVERIFIED_EXISTING_SOURCE', 'Existing file has no identity sidecar; validate/import it separately');
        const identity = JSON.parse(fs.readFileSync(sidecar, 'utf8'));
        if (identity.seriesId !== config.seriesId || identity.vid !== row.vid || identity.episode !== row.episode) {
          throw failure('EXISTING_IDENTITY_MISMATCH', 'Existing source identity does not match the selected episode');
        }
        row.validation = await validateFile(final, config);
        if (identity.sha256 !== row.validation.sha256) throw failure('EXISTING_HASH_MISMATCH', 'Existing source hash does not match its identity sidecar');
        row.status = 'ready'; row.reusedExisting = true; row.completedAt = new Date().toISOString();
        save(); log('READY', { episode: row.episode, reused: true }); return;
      }
      removeTemporary(encrypted); removeTemporary(decoded);
      if (serviceRejected) throw failure('SERVICE_REJECTED_STOP', 'Stopped after service rejection; no alternate source is attempted');
      stage = 'app_source'; row.status = 'resolving_app'; save();
      const media = await fetchAppPlayUrl(row.vid, { quality: '1080p' });
      if (media.source !== 'app' || !meetsDownloadQuality(media)) throw failure('APP_HEVC1080_UNAVAILABLE', 'Exact App 1920x1080 HEVC unavailable; no fallback is permitted');
      if (media.contentKey && !/^[a-f\d]{32}$/i.test(media.contentKey)) throw failure('INVALID_CONTENT_KEY', 'App source could not be used with the native media decoder');
      row.appSource = { source: 'app', quality: '1080p', width: media.width, height: media.height, codec: media.codec, host: new URL(media.url).hostname, encrypted: !!media.contentKey };
      stage = 'media_transfer'; row.status = 'downloading'; save();
      const response = await axios.get(media.url, { ...webMediaRequestOptions(media.url), responseType: 'stream', timeout: 120000,
        headers: { 'User-Agent': media.userAgent, Referer: media.referer, 'Accept-Encoding': 'identity' } });
      const expected = Number(response.headers['content-length']) || 0;
      if (expected > config.maxBytesPerEpisode || ![200, 206].includes(response.status)) {
        response.data.destroy(); throw failure('BAD_MEDIA_RESPONSE', 'CDN returned an unsupported media response');
      }
      if (response.status === 206) {
        const range = /^bytes 0-(\d+)\/(\d+)$/.exec(response.headers['content-range'] || '');
        if (!range || Number(range[1]) + 1 !== Number(range[2])) { response.data.destroy(); throw failure('INCOMPLETE_RANGE', 'CDN returned a partial episode'); }
      }
      let received = 0;
      const meter = new Transform({ transform(chunk, _encoding, callback) {
        received += chunk.length;
        if (received > config.maxBytesPerEpisode) return callback(failure('MEDIA_SIZE_LIMIT', 'Episode exceeds the transfer size limit'));
        callback(null, chunk);
      } });
      await pipeline(response.data, meter, fs.createWriteStream(encrypted, { flags: 'wx' }));
      if (!received || (expected && received !== expected)) throw failure('INCOMPLETE_TRANSFER', 'Episode transfer ended before all bytes arrived');
      row.transfer = { status: response.status, receivedBytes: received, contentLength: expected, complete: true };
      stage = 'native_decode'; row.status = 'validating'; save();
      if (media.contentKey) await runTool(config.ffmpeg || 'ffmpeg', ['-hide_banner', '-loglevel', 'error', '-n', '-decryption_key', media.contentKey,
        '-i', encrypted, '-map', '0:v:0', '-map', '0:a?', '-c', 'copy', '-movflags', '+faststart', '-f', 'mp4', decoded]);
      else fs.copyFileSync(encrypted, decoded, fs.constants.COPYFILE_EXCL);
      row.nativeProcessing = { streamCopy: true, transcoded: false, contentKeyPersisted: false };
      stage = 'full_file_validation'; row.validation = await validateFile(decoded, config);
      // EXCL prevents an unrelated concurrent writer from being overwritten even if it ignores our lock.
      fs.copyFileSync(decoded, final, fs.constants.COPYFILE_EXCL);
      writeJson(sidecar, { schema: 1, seriesId: config.seriesId, episode: row.episode, vid: row.vid, sha256: row.validation.sha256 });
      removeTemporary(decoded); removeTemporary(encrypted);
      row.validation.path = final; row.status = 'ready'; row.completedAt = new Date().toISOString();
      save(); log('READY', { episode: row.episode, width: 1920, height: 1080, codec: 'hevc', sha256: row.validation.sha256 });
    } catch (error) {
      if (isServiceRejection(error)) serviceRejected = true;
      row.status = 'failed'; row.error = safeError(error, stage); row.completedAt = new Date().toISOString();
      save(); log('FAILED', { episode: row.episode, ...row.error });
    }
  }
  try {
    save();
    const metadata = await fetchEpisodeListFromWeb(config.seriesId);
    report.episodes = config.episodes.map(number => {
      const match = metadata.episodes.find(episode => Number(episode.vid_index) === number);
      if (!match || String(match.series_id) !== config.seriesId || !/^\d+$/.test(String(match.vid))) throw failure('MISSING_EPISODE_ID', `Series metadata did not identify episode ${number}`);
      return { episode: number, vid: String(match.vid), seriesId: config.seriesId, status: 'pending' };
    });
    if (new Set(report.episodes.map(row => row.vid)).size !== report.episodes.length) throw failure('DUPLICATE_EPISODE_ID', 'Series metadata returned duplicate episode IDs');
    report.seriesTitle = metadata.series_title; report.seriesEpisodeCount = metadata.episodes.length;
    report.status = 'running'; save();
    let next = 0;
    await Promise.all(Array.from({ length: config.concurrency }, async () => {
      while (next < report.episodes.length) await downloadOne(report.episodes[next++]);
    }));
    report.readyCount = report.episodes.filter(row => row.status === 'ready').length;
    report.failedCount = report.episodes.filter(row => row.status === 'failed').length;
    report.status = report.failedCount ? 'incomplete' : 'complete'; report.completedAt = new Date().toISOString(); save();
    log('COMPLETE', { ready: report.readyCount, failed: report.failedCount, report: reportPath });
    return report.failedCount ? 1 : 0;
  } catch (error) {
    report.status = 'failed'; report.error = safeError(error, 'series_metadata'); report.completedAt = new Date().toISOString(); save();
    log('FATAL', report.error); return 1;
  } finally {
    axios.interceptors.response.eject(interceptor);
    releaseLock(lock);
  }
}

async function main(argv = process.argv.slice(2)) {
  const config = parseArguments(argv);
  if (config.command === 'help') { process.stdout.write(HELP); return 0; }
  if (config.command === 'validate') {
    const result = { status: 'ready', validation: await validateFile(config.source, config) };
    if (config.report) writeJson(config.report, result);
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
    return 0;
  }
  return download(config);
}

if (require.main === module) main().then(code => { process.exitCode = code; }).catch(error => {
  process.stderr.write(JSON.stringify(safeError(error, 'command')) + '\n'); process.exitCode = 1;
});

module.exports = { parseEpisodes, parseArguments, safeError, download, main };
