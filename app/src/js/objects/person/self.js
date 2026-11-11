// You, as the app names you: your short name ("Matt") from your person record.
// Loaded once; until it arrives, or if no person is marked as you, "You".

import { api } from '../../api.js';

let name = null;
let fullName = null;

export const selfReady = api('/api/people/me')
  .then(p => { name = p.short_name || p.name; fullName = p.name; return name; })
  .catch(() => null);

export function selfName() {
  return name || 'You';
}

// Your whole name, as replies to you start with it: "Matthew Thompson, …"
export function selfFullName() {
  return fullName;
}

// After your short name is changed on your page
export function setSelfName(value) {
  name = value || null;
}
