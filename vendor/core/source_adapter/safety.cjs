// SPDX-License-Identifier: GPL-3.0-only
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { failure } = require('./media.cjs');
const frozen = require('./protected-paths.json');

function canonicalPath(value) {
  let existing = path.resolve(value);
  const missing = [];
  while (!fs.existsSync(existing)) {
    const parent = path.dirname(existing);
    if (parent === existing) break;
    missing.unshift(path.basename(existing)); existing = parent;
  }
  // An absent Windows volume has no realpath. Keep its absolute lexical
  // protection instead of making all downloads on other volumes fail ENOENT.
  const normalized = fs.existsSync(existing)
    ? path.resolve(fs.realpathSync(existing), ...missing) : path.resolve(value);
  return process.platform === 'win32' ? normalized.toLowerCase() : normalized;
}

function isInside(value, root) {
  const relative = path.relative(root, value);
  return relative === '' || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith('..' + path.sep));
}

function assertOutputRoot(value) {
  const output = canonicalPath(value);
  for (const original of frozen.paths) {
    const protectedRoot = canonicalPath(path.resolve(__dirname, original));
    if (isInside(output, protectedRoot) || isInside(protectedRoot, output)) {
      throw failure('PROTECTED_OUTPUT_ROOT', 'Output root intersects a frozen original input/output path; choose an isolated directory');
    }
  }
  return path.resolve(value);
}

const identity = stat => [stat.dev, stat.ino, stat.birthtimeNs, stat.mtimeNs, stat.size].map(String).join(':');

function acquireLock(lockPath) {
  let descriptor;
  try { descriptor = fs.openSync(lockPath, 'wx', 0o600); }
  catch (_) { throw failure('OUTPUT_LOCKED', 'Output root has an existing lock; inspect it manually before restarting'); }
  const token = crypto.randomBytes(32).toString('hex');
  try {
    fs.writeSync(descriptor, JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString(), token }));
    fs.fsyncSync(descriptor);
    return { path: lockPath, descriptor, token, identity: identity(fs.fstatSync(descriptor, { bigint: true })), closed: false };
  } catch (_) {
    fs.closeSync(descriptor);
    // An incomplete marker is deliberately left for manual inspection.
    throw failure('LOCK_INITIALIZATION_FAILED', 'Could not initialize output lock; ambiguous marker was preserved');
  }
}

function releaseLock(lock) {
  if (lock.closed) throw failure('LOCK_ALREADY_CLOSED', 'Output lock was already closed');
  try {
    let stat, data;
    try {
      stat = fs.lstatSync(lock.path, { bigint: true });
      data = JSON.parse(fs.readFileSync(lock.path, 'utf8'));
    } catch (_) { throw failure('LOCK_IDENTITY_CHANGED', 'Output lock is missing or ambiguous; no marker was removed'); }
    if (!stat.isFile() || identity(stat) !== lock.identity || data.token !== lock.token
        || identity(fs.fstatSync(lock.descriptor, { bigint: true })) !== lock.identity) {
      throw failure('LOCK_IDENTITY_CHANGED', 'Output lock identity changed; no marker was removed');
    }
    // Keep the acquired file handle open through the final identity check/unlink.
    // No stale, substituted or unparseable marker is ever automatically removed.
    fs.unlinkSync(lock.path);
  } finally {
    fs.closeSync(lock.descriptor); lock.closed = true;
  }
}

module.exports = { canonicalPath, assertOutputRoot, acquireLock, releaseLock };
