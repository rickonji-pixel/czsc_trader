import assert from 'node:assert/strict';
import test from 'node:test';
import {ACCOUNT_REFRESH_SECTIONS, ScopedLoader, actionLabel, actionsForRoute, auditCategoryLabel, auditEventLabel, auditOutcomeLabel, auditQuery, auditScopeLabel, auditSeverityLabel, auditSummary, channelOrderAccountLabel, chartShouldReload, chooseAccountId, comparisonQuery, decisionExecutionLabel, displayFillId, formatBeijingTime, formatPrice, formatQuantity, navigationOptions, orderPriceLabel, parseRoute, qualificationLabel, sideLabel, snapshotFingerprint, statusLabel, systemAlertCount, systemEventLabel} from '../../src/paper_trading_engine/static/app.js';

test('FT-PTEJS01 console state preserves scope, stable polling and Chinese presentation', () => {
  assert.deepEqual(parseRoute('/accounts/s001-v2'), {page: 'account', accountId: 's001-v2'});
  assert.deepEqual(parseRoute('/channels/futu'), {page: 'channel', channel: 'futu'});
  assert.deepEqual(parseRoute('/comparison'), {page: 'comparison'});
  assert.deepEqual(parseRoute('/audit-events'), {page: 'audit'});
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
  assert.equal(systemEventLabel('SERVICE_STARTED'), '服务启动');
  assert.equal(systemAlertCount({alerts: ['x'], scheduler_failures: [{operation: 'x'}]}), 2);
  assert.equal(chooseAccountId('baseline-143', [{account_id: 's001-v1'}], 's001-v1'), 's001-v1');
  assert.deepEqual(navigationOptions('s001-v1'), {showLoading: false, forceRender: false});
  assert.equal(channelOrderAccountLabel({account_id: 's001-v2'}), 's001-v2');
  assert.equal(formatBeijingTime('2026-09-03T11:00:11.806715+00:00'), '2026-09-03 19:00:11');
  assert.equal(displayFillId('9bce6fda-3e47-53b9-b4a6-f52410c6264a'), 'FIL-9BCE6FDA');
  assert.equal(orderPriceLabel({order_type: 'MARKET', limit_price: 0}), '市价');
  assert.equal(orderPriceLabel({payload: {order_type: 'MARKET'}, limit_price: '1.6090'}), '市价');
  assert.equal(orderPriceLabel({order_type: 'LIMIT', limit_price: '1.6500'}), '1.6500');
  assert.equal(actionLabel('WAIT'), '等待');
  assert.equal(actionLabel('HOLD'), '持有');
  assert.equal(sideLabel('SELL'), '卖出');
  assert.equal(qualificationLabel('PAPER_READY'), '获准模拟交易');
  assert.equal(statusLabel('CHANNEL_CASH_MISMATCH'), 'PTE账务现金与Futu现金不一致');
  assert.equal(formatPrice('1.5899999999999999'), '1.590');
  assert.equal(formatQuantity(60500), '60,500 股');
  assert.equal(decisionExecutionLabel({decision_id:'D1',action:'HOLD'}, []), '本次决策无需下单');
  assert.equal(decisionExecutionLabel(
    {decision_id:'D2',action:'SELL'}, [{decision_id:'D2',status:'FILLED_ALL'}],
  ), '全部成交');
  assert.equal(decisionExecutionLabel(
    {decision_id:'D2',action:'SELL'}, [
      {decision_id:'D2',status:'REJECTED'}, {decision_id:'D2',status:'FILLED_ALL'},
    ],
  ), '全部成交（此前 1 次未成功）');
  assert.equal(auditSummary({
    event_type:'ORDER_FILLED',details:{side:'SELL',quantity:60500,average_fill_price:1.589999999999},
  }), '卖出 60,500 股，成交均价 1.590');
  assert.equal(chartShouldReload(null, {scope: {account_id: 'a'}, fingerprint: 'f1'}), true);
  assert.equal(chartShouldReload(
    {account_id: 'a', fingerprint: 'f1'},
    {scope: {account_id: 'a'}, fingerprint: 'f1'},
  ), false);
  assert.equal(chartShouldReload(
    {account_id: 'a', fingerprint: 'f1'},
    {scope: {account_id: 'b'}, fingerprint: 'f1'},
  ), true);
  assert.equal(ACCOUNT_REFRESH_SECTIONS.includes('chart'), false);
  assert.equal(auditCategoryLabel('STRATEGY'), '策略事件');
  assert.equal(auditCategoryLabel('OTHER'), '其他事件');
  assert.equal(auditEventLabel('DECISION_GENERATED'), '生成决策');
  assert.equal(auditEventLabel('ORDER_FILLED'), '订单成交');
  assert.equal(auditEventLabel('BROKER_FEE_RECONCILED'), 'Futu费用对账');
  assert.equal(auditSummary({event_type:'BROKER_FEE_RECONCILED',details:{modeled_fee:'98.0403',actual_fee:'202.4380',adjustment:'-104.3977'}}), '估算费用 98.04，Futu实际费用 202.44，账务调整 -104.40');
  const accounts = [{account_id: 's001-v2', name: 'S001-v2模拟账户'}];
  assert.equal(
    auditScopeLabel({account_id: 's001-v2', channel: 'futu'}, accounts),
    '虚拟账户 · S001-v2模拟账户（s001-v2）',
  );
  assert.equal(auditScopeLabel({account_id: null, channel: 'futu'}, accounts), 'Futu模拟渠道');
  assert.equal(
    auditScopeLabel({event_type: 'DECISION_GENERATED', account_id: null, channel: 'futu'}, accounts),
    '历史记录 · 虚拟账户未记录',
  );
  assert.equal(
    auditScopeLabel({account_id: 's001-v2', channel: 'futu'}, accounts),
    '虚拟账户 · S001-v2模拟账户（s001-v2）',
  );
  assert.equal(auditScopeLabel({account_id: null, channel: null}, accounts), '历史记录 · 作用域未记录');
  assert.equal(auditSeverityLabel('INFO'), '信息');
  assert.equal(auditOutcomeLabel('SUCCESS'), '成功');
  assert.equal(
    auditQuery({category: 'TRADING', account_id: 's001-v1', correlation_id: 'DEC 1'}),
    'category=TRADING&account_id=s001-v1&correlation_id=DEC+1',
  );
});
