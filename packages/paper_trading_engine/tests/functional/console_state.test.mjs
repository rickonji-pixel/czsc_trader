import assert from 'node:assert/strict';
import test from 'node:test';
import {ScopedLoader, actionsForRoute, channelOrderAccountLabel, chooseAccountId, comparisonQuery, formatBeijingTime, navigationOptions, parseRoute, snapshotFingerprint, systemAlertCount, systemEventLabel} from '../../src/paper_trading_engine/static/app.js';

test('FT-PTEJS01 console state preserves scope, stable polling and Chinese presentation', () => {
  assert.deepEqual(parseRoute('/accounts/s001-v2'), {page: 'account', accountId: 's001-v2'});
  assert.deepEqual(parseRoute('/channels/futu'), {page: 'channel', channel: 'futu'});
  assert.deepEqual(parseRoute('/comparison'), {page: 'comparison'});
  const loader = new ScopedLoader();
  const oldRequest = loader.begin('baseline-143');
  const newRequest = loader.begin('s001-v2');
  assert.equal(loader.accept(oldRequest, 'baseline-143'), false);
  assert.equal(loader.accept(newRequest, 's001-v2'), true);
  assert.deepEqual(actionsForRoute({page: 'account'}), ['pause', 'resume']);
  assert.deepEqual(actionsForRoute({page: 'channel'}), ['pause', 'resume', 'cancel']);
  assert.equal(comparisonQuery(['alpha', 'beta']), 'account_id=alpha&account_id=beta');
  const first = {as_of: '2026-09-04T09:00:00Z', account: {cash: '100000.0000'}};
  const later = {as_of: '2026-09-04T09:00:05Z', account: {cash: '100000.0000'}};
  assert.equal(snapshotFingerprint(first), snapshotFingerprint(later));
  assert.equal(systemEventLabel('DATA_PUBLICATION_FAILED'), '发布数据失败');
  assert.equal(systemAlertCount({alerts: ['x'], scheduler_failures: [{operation: 'x'}]}), 2);
  assert.equal(chooseAccountId('baseline-143', [{account_id: 's001-v1'}], 's001-v1'), 's001-v1');
  assert.deepEqual(navigationOptions('s001-v1'), {showLoading: false, forceRender: false});
  assert.equal(channelOrderAccountLabel({}), '历史未记录');
  assert.equal(formatBeijingTime('2026-09-03T11:00:11.806715+00:00'), '2026-09-03 19:00:11');
});
