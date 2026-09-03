import assert from 'node:assert/strict';
import test from 'node:test';
import {ScopedLoader, actionsForRoute, chooseAccountId, comparisonQuery, parseRoute, snapshotFingerprint, systemAlertCount, systemEventLabel} from '../../src/paper_trading_engine/static/app.js';

test('routes preserve the complete resource identity', () => {
  assert.deepEqual(parseRoute('/accounts/s001-v2'), {page: 'account', accountId: 's001-v2'});
  assert.deepEqual(parseRoute('/channels/futu'), {page: 'channel', channel: 'futu'});
  assert.deepEqual(parseRoute('/comparison'), {page: 'comparison'});
});

test('a late response cannot overwrite the new account', () => {
  const loader = new ScopedLoader();
  const oldRequest = loader.begin('baseline-143');
  const newRequest = loader.begin('s001-v2');
  assert.equal(loader.accept(oldRequest, 'baseline-143'), false);
  assert.equal(loader.accept(newRequest, 's001-v2'), true);
});

test('interventions only appear in their owning resource', () => {
  assert.deepEqual(actionsForRoute({page: 'account'}), ['pause', 'resume']);
  assert.deepEqual(actionsForRoute({page: 'comparison'}), []);
  assert.deepEqual(actionsForRoute({page: 'channel'}), ['pause', 'resume', 'cancel']);
});

test('comparison repeats account_id query parameters', () => {
  assert.equal(comparisonQuery(['alpha', 'beta']), 'account_id=alpha&account_id=beta');
});

test('polling ignores timestamps but detects material state changes', () => {
  const first = {as_of: '2026-09-04T09:00:00Z', account: {cash: '100000.0000'}, orders: []};
  const later = {as_of: '2026-09-04T09:00:05Z', account: {cash: '100000.0000'}, orders: []};
  const changed = {as_of: '2026-09-04T09:00:10Z', account: {cash: '90000.0000'}, orders: []};
  assert.equal(snapshotFingerprint(first), snapshotFingerprint(later));
  assert.notEqual(snapshotFingerprint(first), snapshotFingerprint(changed));
});

test('system event history uses precise Chinese labels', () => {
  assert.equal(systemEventLabel('DATA_PUBLICATION_FAILED'), '发布数据失败');
  assert.equal(systemEventLabel('DATA_PUBLISHED'), '数据发布成功');
  assert.equal(systemEventLabel('UNKNOWN_EVENT'), 'UNKNOWN_EVENT');
});

test('system alert count includes active alerts and failed operations', () => {
  assert.equal(systemAlertCount({alerts: ['DATA_PUBLICATION_FAILED'], scheduler_failures: [{operation: 'publication'}]}), 2);
  assert.equal(systemAlertCount({alerts: [], scheduler_failures: []}), 0);
});

test('a stale account URL falls back to the current default account', () => {
  const accounts = [{account_id: 's001-v1'}, {account_id: 's001-v2'}];
  assert.equal(chooseAccountId('baseline-143', accounts, 's001-v1'), 's001-v1');
  assert.equal(chooseAccountId('s001-v2', accounts, 's001-v1'), 's001-v2');
});
