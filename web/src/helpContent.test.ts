import assert from "node:assert/strict";
import { releaseNotes, searchHelp, startHere, userGuide, validateHelpContent } from "./helpContent";

const errors = validateHelpContent(userGuide, releaseNotes);
assert.deepEqual(errors, [], errors.join("\n"));
assert.equal(userGuide.sections.length > 0, true);
assert.equal(releaseNotes.releases.length > 0, true);
assert.equal(releaseNotes.releases[0].status, "current");
assert.equal(startHere.steps.length, 6);
assert.equal(startHere.concepts.every((item) => item.body.en && item.body.zh), true);
assert.equal(searchHelp("en", "monitor").some((section) => section.items.length > 0), true);
assert.deepEqual(searchHelp("zh", "不存在的帮助关键词"), []);
console.log(`help content contract: ${userGuide.sections.length} guide sections · ${releaseNotes.releases.length} releases`);
