const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const code = fs.readFileSync(path.join(__dirname, '../integrations/homer/osd-summary.js'), 'utf8');
  const {pointConfirmed, summarizeEvents} = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
  const confirmed = {match_evidence:{version:2,scope:'confirmed_point',point_confirmed:true}};
  const candidates = [{}, {...confirmed,verification_pending:true}, {...confirmed,location_review_required:true},
    {match_evidence:{version:2,scope:'candidate',point_confirmed:true}},
    {match_evidence:{version:1,scope:'confirmed_point',point_confirmed:true}}];
  assert.equal(pointConfirmed(confirmed), true);
  for (const event of candidates) assert.equal(Boolean(pointConfirmed(event)), false);
  const summary = summarizeEvents([confirmed,...candidates]);
  assert.equal(summary.confirmed,1);
  assert.equal(summary.candidates,5);
  console.log('SNUT: pewność dopasowania, oczekiwanie i przegląd - OK');
})().catch(error => { console.error(error); process.exitCode=1; });
