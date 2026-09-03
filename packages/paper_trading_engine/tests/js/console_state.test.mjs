import assert from 'node:assert/strict';
import test from 'node:test';
import {ScopedLoader, actionsForRoute, comparisonQuery, parseRoute} from '../../src/paper_trading_engine/static/app.js';

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
