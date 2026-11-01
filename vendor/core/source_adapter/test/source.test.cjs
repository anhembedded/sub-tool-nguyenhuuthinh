// SPDX-License-Identifier: GPL-3.0-only
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { parseEpisodes, parseArguments } = require('../cli.cjs');
const { validateProbe } = require('../media.cjs');
const { meetsDownloadQuality } = require('../native/download-quality');
const { assertOutputRoot, acquireLock, releaseLock } = require('../safety.cjs');
const protectedPaths = require('../protected-paths.json').paths.map(p => path.resolve(__dirname, '..', p));

test('source policy never treats a minimum short side as exact resolution or codec', () => {
  assert.equal(meetsDownloadQuality({ width: 1920, height: 1080, codec: 'hevc' }), true);
  for (const row of [
    { width: 854, height: 480, codec: 'hevc' },
    { width: 3840, height: 2160, codec: 'hevc' },
    { width: 1920, height: 1080, codec: 'bytevc2' },
    { width: 1920, height: 1080, codec: 'h264' },
    { width: 1080, height: 1920, codec: 'hevc' },
  ]) assert.equal(meetsDownloadQuality(row), false);
  assert.throws(() => meetsDownloadQuality({ width: 1920, height: 1080, codec: 'hevc' }, '480p'));
});

test('CLI requires isolated explicit output and validates requested episode range', () => {
  assert.deepEqual(parseEpisodes('1-3,2,10'), [1, 2, 3, 10]);
  for (const invalid of ['3-1', '0', '1,x', '', '1-10000']) assert.throws(() => parseEpisodes(invalid));
  assert.throws(() => parseArguments(['download', '--series-id', '12', '--episodes', '1']));
  assert.throws(() => parseArguments(['download', '--series-id', '12', '--episodes', '1', '--output-root', '.', '--concurrency', '5']));
  assert.equal(parseArguments(['--help']).command, 'help');
  assert.throws(() => parseArguments(['validate', '--source', 'video.mp4', '--report', 'video.mp4']), /overwrite source/);
});

test('probe gate rejects unsupported frame metadata or missing source audio', () => {
  const video = { codec_type: 'video', codec_name: 'hevc', width: 1920, height: 1080, avg_frame_rate: '30/1', r_frame_rate: '30/1', nb_frames: '90' };
  const probe = { streams: [video, { codec_type: 'audio', codec_name: 'aac' }], format: { duration: '3' } };
  assert.equal(validateProbe(probe).frameCount, 90);
  assert.throws(() => validateProbe({ ...probe, streams: [video] }), /audio/);
  assert.throws(() => validateProbe({ ...probe, streams: [{ ...video, avg_frame_rate: '24/1' }, probe.streams[1]] }), /30\/1/);
  assert.throws(() => validateProbe({ ...probe, streams: [{ ...video, nb_frames: undefined }, probe.streams[1]] }), /frame count/);
});

test('download roots cannot contain, equal or descend into frozen original roots', () => {
  for (const root of protectedPaths) {
    for (const forbidden of [root, path.join(root, 'nested'), path.dirname(root)]) {
      assert.throws(() => assertOutputRoot(forbidden), /frozen original/);
      assert.throws(() => parseArguments(['download', '--series-id', '12', '--episodes', '1', '--output-root', forbidden]), /frozen original/);
    }
  }
  const isolated = path.join(os.tmpdir(), 'workflow-source-isolated');
  assert.equal(assertOutputRoot(isolated), path.resolve(isolated));
});

test('lock release only deletes the original token and original file identity', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hongguo-source-lock-'));
  const lockPath = path.join(directory, '.download.lock');
  try {
    let lock = acquireLock(lockPath);
    assert.equal(lock.token.length, 64);
    assert.throws(() => acquireLock(lockPath), /existing lock/);
    releaseLock(lock);
    assert.equal(fs.existsSync(lockPath), false);

    lock = acquireLock(lockPath);
    const changed = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
    changed.token = '0'.repeat(64);
    fs.writeFileSync(lockPath, JSON.stringify(changed));
    assert.throws(() => releaseLock(lock), /identity changed/);
    assert.equal(fs.existsSync(lockPath), true);
    fs.unlinkSync(lockPath);

    lock = acquireLock(lockPath);
    fs.unlinkSync(lockPath);
    fs.writeFileSync(lockPath, JSON.stringify({ token: lock.token }));
    assert.throws(() => releaseLock(lock), /identity changed/);
    assert.equal(fs.existsSync(lockPath), true);
    fs.unlinkSync(lockPath);
  } finally {
    // Only this test-owned marker/directory is removed; never recurse over inputs.
    if (fs.existsSync(lockPath)) fs.unlinkSync(lockPath);
    fs.rmdirSync(directory);
  }
});

test('App selector preserves signer but fails closed on bytevc2, H264, upscaling and mislabeled renditions', async () => {
  const axiosPath = require.resolve('axios');
  const original = require.cache[axiosPath];
  const vid = '7696095984266251326';
  let inventory = [];
  const mock = async options => ({ status: 200, headers: {}, data: JSON.stringify(options.method === 'POST'
    ? { data: { [vid]: { video_model: { fallback_api: 'https://vas-lf-x.snssdk.com/model' } } } }
    : { video_info: { data: { video_list: Object.fromEntries(inventory.map((row, index) => [index, row])) } } }) });
  require.cache[axiosPath] = { id: axiosPath, filename: axiosPath, loaded: true, exports: mock };
  const { fetchAppPlayUrl } = require('../native/app-source');
  const stream = (codec_type, vwidth, vheight) => ({ codec_type, vwidth, vheight, definition: '1080p', main_url: 'https://v3.qznovelvod.com/video.mp4' });
  try {
    for (const row of [stream('bytevc2', 1920, 1080), stream('h264', 1920, 1080), stream('hevc', 3840, 2160), stream('hevc', 854, 480)]) {
      inventory = [row]; await assert.rejects(fetchAppPlayUrl(vid, { quality: '1080p' }));
    }
    inventory = [stream('h264', 1920, 1080), stream('bytevc1', 1920, 1080)];
    const selected = await fetchAppPlayUrl(vid, { quality: '1080p' });
    assert.equal(selected.codec, 'hevc');
    assert.equal(selected.width, 1920); assert.equal(selected.height, 1080);
    assert.equal(selected.variants.length, 1);
  } finally {
    if (original) require.cache[axiosPath] = original; else delete require.cache[axiosPath];
  }
});
