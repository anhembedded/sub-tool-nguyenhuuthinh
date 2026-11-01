// SPDX-License-Identifier: GPL-3.0-only
// Extracted from jackotom/hongguo-drama src/native/hongguo.js at local
// revision 84b6e88e65374942c123a53c7d6ad9e78bcd101b. Metadata only.
'use strict';
const axios = require('axios');
const SITE_ORIGIN = 'https://hongguoduanju.com';
const WEB_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36';
const WEB_REFERER = SITE_ORIGIN + '/';

/** 从响应体里安全地取出某个字符串字段 */
function pickJsonString(text, key) {
  const m = text.match(new RegExp('"' + key + '"\\s*:\\s*"([^"]*)"'));
  return m ? m[1].replace(/\\u002F/g, '/').replace(/\\u0026/g, '&') : '';
}

/** 拉取官网页面 HTML */
async function fetchSiteHtml(url, timeout = 30000) {
  const res = await axios.get(url, {
    headers: {
      'User-Agent': WEB_UA,
      'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
      'Accept-Language': 'zh-CN,zh;q=0.9',
      'Referer': WEB_REFERER,
    },
    timeout,
    responseType: 'text',
    maxRedirects: 5,
  });
  return typeof res.data === 'string' ? res.data : JSON.stringify(res.data);
}

/**
 * 从官网 player 页内嵌 JSON 取完整分集列表
 * vid_list 按下标即集数顺序（下标 i → 第 i+1 集）
 */
async function fetchEpisodeListFromWeb(seriesId) {
  const html = await fetchSiteHtml(`${SITE_ORIGIN}/player/${encodeURIComponent(seriesId)}`);

  const m = html.match(/"vid_list"\s*:\s*\[([^\]]*)\]/);
  if (!m) throw new Error('官网页面未找到分集数据（vid_list）');

  const vids = m[1]
    .split(',')
    .map((s) => s.trim().replace(/^"|"$/g, ''))
    .filter((s) => /^\d+$/.test(s));
  if (!vids.length) throw new Error('官网分集列表为空');

  const seriesTitle = pickJsonString(html, 'series_name') || '未命名短剧';
  const cover = pickJsonString(html, 'series_cover');
  const accessible = html.match(/"accessible_episode_cnt"\s*:\s*(\d+)/);
  const webAccessibleEpisodes = accessible ? Math.min(Number(accessible[1]), vids.length) : null;

  const episodes = vids.map((vid, i) => ({
    vid,
    vid_index: i + 1,
    title: '',
    series_id: String(seriesId),
    series_title: seriesTitle,
    cover,
    web_available: webAccessibleEpisodes === null ? null : i < webAccessibleEpisodes,
  }));

  return {
    series_id: String(seriesId),
    series_title: seriesTitle,
    cover: cover || (episodes[0] && episodes[0].cover) || '',
    total: episodes.length,
    web_accessible_episodes: webAccessibleEpisodes,
    episodes,
    source: 'web', // 标记来源，便于排查
  };
}


module.exports = { fetchEpisodeListFromWeb };
