// SPDX-License-Identifier: GPL-3.0-only
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { meetsDownloadQuality } = require('./native/download-quality');

function failure(code, message) { return Object.assign(new Error(message), { code, safeMessage: true }); }

function runTool(command, args, { capture = false, timeoutMs = 300000 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { windowsHide: true, stdio: ['ignore', capture ? 'pipe' : 'ignore', 'ignore'] });
    let output = '', timedOut = false, tooMuchOutput = false;
    if (capture) child.stdout.on('data', chunk => {
      output += chunk;
      if (output.length > 4 * 1024 * 1024) { tooMuchOutput = true; child.kill('SIGKILL'); }
    });
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, timeoutMs);
    child.once('error', () => { clearTimeout(timer); reject(failure('MEDIA_TOOL_LAUNCH_FAILED', 'Could not start the media validation tool')); });
    child.once('close', code => {
      clearTimeout(timer);
      if (timedOut) reject(failure('MEDIA_TOOL_TIMEOUT', 'Media validation exceeded its time limit'));
      else if (tooMuchOutput) reject(failure('MEDIA_TOOL_OUTPUT_LIMIT', 'Media probe output exceeded its bound'));
      else if (code !== 0) reject(failure('MEDIA_TOOL_FAILED', `Media validation exited with code ${code}`));
      else resolve(output);
    });
  });
}

async function sha256(file) {
  const hash = crypto.createHash('sha256');
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}

function validateProbe(probe) {
  const videos = (probe.streams || []).filter(stream => stream.codec_type === 'video');
  const audios = (probe.streams || []).filter(stream => stream.codec_type === 'audio');
  const video = videos[0], audio = audios[0];
  if (videos.length !== 1 || !video || !meetsDownloadQuality(video) || !(Number(probe.format?.duration) > 0)) {
    throw failure('INVALID_HEVC1080_FILE', 'File must contain one exact 1920x1080 HEVC video stream with positive duration');
  }
  if (video.avg_frame_rate !== '30/1' || video.r_frame_rate !== '30/1') {
    throw failure('UNVALIDATED_FRAME_RATE', 'The confirmed OCR template requires 30/1 nominal and average frame rates');
  }
  if (!audio) throw failure('MISSING_AUDIO', 'Downloaded episode has no audio track');
  const frameCount = Number(video.nb_frames);
  if (!Number.isSafeInteger(frameCount) || frameCount <= 0) {
    throw failure('MISSING_FRAME_COUNT', 'The validated source must expose a positive integral frame count');
  }
  return { video, audio, frameCount };
}

async function validateFile(file, tools = {}) {
  const absolute = path.resolve(file);
  const probe = JSON.parse(await runTool(tools.ffprobe || 'ffprobe', ['-v', 'error', '-show_entries',
    'format=duration,size,format_name:stream=index,codec_name,codec_type,width,height,avg_frame_rate,r_frame_rate,nb_frames,duration,duration_ts,time_base,start_time,sample_rate,channels:stream_tags=language,title',
    '-of', 'json', absolute], { capture: true }));
  const { video, audio, frameCount } = validateProbe(probe);
  await runTool(tools.ffmpeg || 'ffmpeg', ['-hide_banner', '-v', 'error', '-xerror', '-i', absolute, '-f', 'null', '-']);
  return { path: absolute, bytes: fs.statSync(absolute).size, sha256: await sha256(absolute),
    width: video.width, height: video.height, frameRate: video.avg_frame_rate, frameCount,
    videoDuration: Number(video.duration) || null, containerDuration: Number(probe.format.duration),
    videoCodec: video.codec_name, audioCodec: audio.codec_name,
    audioSampleRate: Number(audio.sample_rate), audioChannels: audio.channels,
    fullDecodeExitCode: 0, confirmed1080p: true, probe,
    timingNote: '30/1 stream metadata plus full decode; per-frame PTS validation remains a separate canonical QA gate' };
}

module.exports = { failure, runTool, sha256, validateProbe, validateFile };
