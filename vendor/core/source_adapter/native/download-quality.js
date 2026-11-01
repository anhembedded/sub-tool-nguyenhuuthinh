// SPDX-License-Identifier: GPL-3.0-only
'use strict';

function normalizeDownloadQuality(value = '1080p') {
  if (value !== '1080p') throw new Error('This validated adapter supports exact 1920x1080 HEVC only');
  return value;
}

function meetsDownloadQuality({ width, height, codec, codec_name: codecName } = {}, quality = '1080p') {
  normalizeDownloadQuality(quality);
  return Number(width) === 1920 && Number(height) === 1080
    && ['hevc', 'h265', 'hvc1', 'hev1', 'bytevc1'].includes(String(codec || codecName || '').toLowerCase());
}

module.exports = { normalizeDownloadQuality, meetsDownloadQuality };
