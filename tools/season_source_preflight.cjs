'use strict';
// Resolve permitted source metadata and HEAD sizes only. Never persist URLs/keys.
const fs = require('node:fs');
const path = require('node:path');
const adapter = path.join(__dirname, '../vendor/core/source_adapter');
const axios = require(path.join(adapter, 'node_modules/axios'));
const { fetchEpisodeListFromWeb } = require(path.join(adapter, 'native/episode-metadata'));
const { fetchAppPlayUrl } = require(path.join(adapter, 'native/app-source'));
const { webMediaRequestOptions } = require(path.join(adapter, 'native/web-media-guard'));
const { meetsDownloadQuality } = require(path.join(adapter, 'native/download-quality'));

async function main(root) {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json'), 'utf8'));
  const begin = new Date().toISOString();
  let meta;
  for (let attempt=0; attempt<2; attempt++) {
    try { meta = await fetchEpisodeListFromWeb(manifest.series_id); break; }
    catch(e) {
      if (attempt || [401,403,429].includes(Number(e.status || e.response?.status))) throw e;
    }
  }
  if (meta.total !== manifest.episodes.length || new Set(meta.episodes.map(e => e.vid)).size !== meta.total)
    throw new Error('Catalog differs from whole-season manifest');
  const results = [];
  let rejected = false;
  for (const number of manifest.episodes) {
    if (rejected) { results.push({ episode: number, status: 'NOT_ATTEMPTED_AFTER_REJECTION' }); continue; }
    const row = meta.episodes.find(e => e.vid_index === number);
    try {
      const media = await fetchAppPlayUrl(row.vid, { quality: '1080p' });
      if (!meetsDownloadQuality(media)) throw Object.assign(new Error(), { code: 'APP_HEVC1080_UNAVAILABLE' });
      const head = await axios.head(media.url, { ...webMediaRequestOptions(media.url), timeout: 30000,
        headers: { 'User-Agent': media.userAgent, Referer: media.referer, 'Accept-Encoding': 'identity' } });
      const bytes = Number(head.headers['content-length']);
      if (!Number.isSafeInteger(bytes) || bytes <= 0) throw Object.assign(new Error(), { code: 'UNKNOWN_CONTENT_LENGTH' });
      results.push({ episode: number, vid: row.vid, status: 'AVAILABLE_METADATA_ONLY', bytes,
        duration_seconds: media.durationSeconds > 0 ? media.durationSeconds : null,
        width: media.width, height: media.height, codec: media.codec });
    } catch (e) {
      const status = Number(e.status || e.response?.status) || null;
      const code = /^[A-Z0-9_]+$/.test(e.code || '') ? e.code : 'PREFLIGHT_FAILED';
      results.push({ episode: number, vid: row.vid, status: 'FAILED', error_code: code, http_status: status });
      rejected = [401, 403, 429].includes(status) || ['BUSINESS_REJECTED', 'SECURITY_BLOCKED'].includes(code);
    }
    console.log(JSON.stringify(results.at(-1)));
  }
  const report = { schema: 1, begin_utc: begin, end_utc: new Date().toISOString(),
    status: results.every(r => r.status === 'AVAILABLE_METADATA_ONLY') ? 'PASS_METADATA_ONLY' : 'BLOCKED',
    series_id: manifest.series_id, series_title: meta.series_title, catalog_total: meta.total,
    web_accessible_episodes: meta.web_accessible_episodes, source_bytes: results.reduce((a, r) => a + (r.bytes || 0), 0),
    duration_seconds: results.every(r => r.duration_seconds > 0) ? results.reduce((a, r) => a + r.duration_seconds, 0) : null,
    episodes: results, note: 'HEAD and source resolution only; full transfer/hash/decode still required.' };
  fs.mkdirSync(path.join(root, 'preflight'), { recursive: true });
  const file = path.join(root, 'preflight', `sources-${Date.now()}.json`);
  fs.writeFileSync(file, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
  fs.writeFileSync(path.join(root, 'source-preflight.json'), JSON.stringify({ report: file }, null, 2));
  if (report.status !== 'PASS_METADATA_ONLY') process.exitCode = 1;
}
main(path.resolve(process.argv[2])).catch(e => {
  const root=path.resolve(process.argv[2]);
  const directory=path.join(root,'preflight');fs.mkdirSync(directory,{recursive:true});
  const file=path.join(directory,`sources-failed-${Date.now()}.json`);
  const report={status:'BLOCKED',stage:'catalog_preflight',end_utc:new Date().toISOString(),
    error_code:/^[A-Z0-9_]+$/.test(e.code || '')?e.code:'PREFLIGHT_FAILED',
    http_status:Number(e.status || e.response?.status)||null,
    note:'No sensitive response, URL, signature or key persisted.'};
  fs.writeFileSync(file,JSON.stringify(report,null,2),{flag:'wx'});
  fs.writeFileSync(path.join(root,'source-preflight.json'),JSON.stringify({report:file},null,2));
  console.error(JSON.stringify(report));process.exitCode=1;
});
