// Item edits the sdlc-kit to-do CLI lacks, through the kit core's locked update.
// Usage: node todo_item.mjs retitle <id> <title>
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const kit = process.env.SDLC_KIT || path.join(os.homedir(), '.agents/skills/sdlc-kit');
const core = await import(pathToFileURL(path.join(kit, 'todo', 'core.mjs')).href);
const [command, id, title] = process.argv.slice(2);
if (command === 'retitle' && id && title) {
  await core.update(core.todoPath(), id, item => {
    item.title = title.replace(/[\r\n]+/g, ' ').trim();
    item.events.push({ at: core.stamp(), who: 'factory', what: 'retitled' });
  });
} else {
  console.error('usage: todo_item.mjs retitle <id> <title>');
  process.exit(2);
}
