'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const results = require('../../src/ear_api/static/results.js');
const { core } = require('../../src/ear_api/static/workspace.js');
const id = '0199f0a0-0009-7aaa-9bbb-000000000001';
const ref = '0199f0a0-0002-7aaa-9bbb-000000000002';

test('results route accepts date and stable ID only; rejects invalid dates and versions', () => {
  assert.deepEqual(results.routeOf('?date=2026-02-30&activity=bad&version=1&token=hidden'), { date: null, week: null, id: null, version: null, timeline:false, source:'all' });
  assert.deepEqual(results.routeOf('?date=2024-02-29&activity=' + id + '&version=2'), { date: '2024-02-29', week: null, id, version: 2, timeline:false, source:'all' });
  assert.equal(results.routeOf('?activity=' + id + '&version=2147483648').version, null);
  assert.equal(results.href({ date: '2026-11-25', id, version: 2, token: 'private' }), '/results/?date=2026-11-25&activity=' + id + '&version=2');
  assert.equal(results.routeOf('?week=2026-11-01').week, '2026-10-26');
  assert.equal(results.routeOf('?week=2026-10-26&date=2026-11-02').week, null);
  assert.equal(results.href({ week: '2026-10-26' }), '/results/?week=2026-10-26');
});

test('calendar weeks are Monday to Sunday across years and DST', () => {
  assert.deepEqual(results.datesFor('2027-01-01'), ['2026-12-28','2026-12-29','2026-12-30','2026-12-31','2027-01-01','2027-01-02','2027-01-03']);
  assert.deepEqual(results.datesFor('2026-11-01'), ['2026-10-26','2026-10-27','2026-10-28','2026-10-29','2026-10-30','2026-10-31','2026-11-01']);
  assert.equal(results.weekStart('2026-11-02'), '2026-11-02');
  assert.equal(results.weekStart('2026-02-29'), null);
  assert.deepEqual(results.datesFor('1899-12-31'), []);
  assert.equal(results.datesFor('9999-12-26').at(-1), '9999-12-26');
});

test('notes match an explicit activity ID and current status; cursors cannot repeat', () => {
  const n = { id: 'note', current: true, payload: { activity_id: id, evidence_ids: [ref] } };
  assert.deepEqual(results.notesFor([n, { ...n, current: false }, { ...n, payload: { activity_id: 'other' } }], id), [n]);
  const seen = new Set();
  assert.equal(results.pageCursor(seen, 'first'), true);
  assert.equal(results.pageCursor(seen, 'second'), true);
  assert.equal(results.pageCursor(seen, 'first'), false);
  assert.equal(results.pageCursor(seen, null), true);
});


test('coverage slots join adjacent bins and clip the last to the day boundary', () => {
  const range = {start:'2026-10-29T00:00:00Z',end:'2026-10-29T00:18:00Z'};
  const start = Date.parse(range.start);
  assert.deepEqual(results.coverageSlots(range, '1101'), [[start,start+600000],[start+900000,Date.parse(range.end)]]);
  assert.equal(results.coverageSlots(range, '11x1'), null);
  assert.equal(results.coverageSlots(range, '111'), null);
});

test('week coverage validates each daylight saving day and treats unavailable separately from empty', () => {
  const zone = core.makeZone('local', ms => ms < Date.parse('2026-03-08T07:00:00Z') || ms >= Date.parse('2026-11-01T06:00:00Z') ? -300 : -240);
  const days = ['2026-03-08','2026-11-01'].map(d=>core.dayBounds(d,zone));
  const value = {slot_seconds:300,timeline:{state:'partial',days:['1'+'0'.repeat(275),'0'.repeat(299)+'1']},activity:{state:'unavailable',days:['','']}};
  const tracks = results.weekTracks(value, days);
  assert.deepEqual(tracks[1][0].spans[0],[Date.parse(days[1].end)-300000,Date.parse(days[1].end)]);
  assert.equal(tracks[0][1].state,'unavailable');
  value.timeline.days[1] = '0'.repeat(288);
  assert.equal(results.weekTracks(value,days),null);
});

test('source timeline deep links keep date and the allowed source, not activity or arbitrary filters', () => {
  const d = '2026-11-25';
  assert.deepEqual(results.routeOf(`?date=${d}&view=timeline&source=librechat&activity=${id}`),
    {date:d, week:null, id:null, version:null, timeline:true, source:'librechat'});
  assert.equal(results.routeOf('?view=timeline&source=untrusted').source, 'all');
  assert.equal(results.href({date:d, timeline:true, source:'legacy-ear', id}), `/results/?date=${d}&view=timeline&source=legacy-ear`);
});

test('results routes are GET allowlisted only for the opted-in shared client', () => {
  const { client } = require('../../src/ear_api/static/workspace.js');
  for (const path of ['/v1/timeline', '/v1/week-coverage', '/v1/source-timeline']) {
    assert.equal(client.pathOk(path), false);
    assert.equal(client.pathOk(path, true), true);
  }
  assert.equal(client.pathOk('/v1/feedback', true), false);
  assert.equal(client.pathOk('//outside.invalid/v1/timeline', true), false);
});

test('source streams merge in true timestamp order within a millisecond', () => {
  const b = '2026-10-29T00:00:00';
  const first = {start:b+'.000001Z', id:'z'};
  const second = {start:b+'.000002Z', id:'a'};
  assert.deepEqual([second, first].sort(results.chronological), [first, second]);
  assert.equal(results.chronological({start:b+'Z', id:'z'}, {start:b+'.000000Z', id:'a'}) > 0, true);
});
