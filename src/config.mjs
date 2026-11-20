import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

export const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const defaults = { port: 8765, remoteHost: '', remoteRoot: '', remotePython: '' };

function readConfig() {
  let contents;
  try {
    contents = readFileSync(path.join(ROOT, 'studio.config.json'), 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') return { ...defaults };
    throw error;
  }
  const overrides = JSON.parse(contents);
  if (!overrides || typeof overrides !== 'object' || Array.isArray(overrides)) {
    throw new Error('studio.config.json must contain a JSON object');
  }
  return { ...defaults, ...overrides };
}

export const config = readConfig();

export function validId(value) {
  return typeof value === 'string' && /^[a-zA-Z0-9_-]{1,80}$/.test(value);
}

export function quote(value) {
  return "'" + String(value).replaceAll("'", "'\\''") + "'";
}
