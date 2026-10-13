import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildDuplexClearUpdates,
  buildDuplexUpdates,
  createDuplexDraft,
  duplexPresetFields,
  missingDuplexField,
  readDuplexSettings,
  selectDuplexPreset,
  selectDuplexProvider,
} from '../node_modules/.cache/settings-refactor/modules/agent/a2aDuplexConfig.js';
import { normalizeSettingsConfigUpdates } from '../node_modules/.cache/settings-refactor/services/settingsContract.js';

test('each preset shows only its own fields', () => {
  assert.deepEqual(duplexPresetFields('team_model'), ['model_name']);
  assert.deepEqual(duplexPresetFields('jev'), ['endpoint', 'model', 'api_key']);
  assert.deepEqual(duplexPresetFields('clef'), ['account_id', 'api_key', 'model']);
  for (const preset of ['mindshub', 'openai', 'custom']) {
    assert.deepEqual(duplexPresetFields(preset), ['api_base', 'model', 'api_key']);
  }
});

test('legacy backend values map to kind and preset', () => {
  const cases = [
    [{ duplex_router_backend: 'sdk', duplex_router_model_name: 'team-a' }, 'logprob_llm', 'team_model', 'team-a'],
    [{ duplex_router_backend: 'mindshub' }, 'logprob_llm', 'mindshub', 'mindshub_air'],
    [{ duplex_router_backend: 'jev', duplex_router_model_name: 'jev-2' }, 'typed_choice', 'jev', 'jev-2'],
    [{ duplex_router_backend: 'clef', duplex_router_model: 'clef-flash' }, 'typed_choice', 'clef', 'clef-flash'],
    [{}, 'logprob_llm', 'team_model', ''],
  ];
  for (const [values, provider, preset, model] of cases) {
    const view = readDuplexSettings(values);
    assert.equal(view.provider, provider);
    assert.equal(view.preset, preset);
    assert.equal(preset === 'team_model' ? view.model_name : view.model, model);
  }
  const jev = readDuplexSettings({
    duplex_router_backend: 'jev',
    duplex_router_api_base: 'https://j.example/v1/',
    duplex_router_endpoint_path: 'decisions',
  });
  assert.equal(jev.endpoint, 'https://j.example/v1/decisions');
});

test('new provider values are read as saved', () => {
  const view = readDuplexSettings({
    duplex_router_enabled: 'true',
    duplex_router_mode: 'active',
    duplex_router_provider: 'typed_choice',
    duplex_router_typed_preset: 'clef',
    duplex_router_typed_model: 'clef-flash',
    duplex_router_account_id: 'acct',
    duplex_router_api_key: 'secret',
  });
  assert.equal(view.enabled, true);
  assert.equal(view.preset, 'clef');
  assert.equal(view.model, 'clef-flash');
  assert.equal(view.account_id, 'acct');
  assert.equal(view.hasApiKey, true);
});

test('switching kind and preset loads preset defaults and saves only relevant keys', () => {
  const saved = readDuplexSettings({ duplex_router_provider: 'logprob_llm', duplex_router_llm_preset: 'team_model' });
  let draft = { ...createDuplexDraft(saved), mode: 'active' };
  assert.equal(missingDuplexField(draft), 'model_name');

  draft = selectDuplexProvider(draft, 'typed_choice', saved);
  assert.equal(draft.preset, 'jev');
  assert.equal(draft.endpoint, 'https://api.typesafe.ai/v1/systemone');
  const jevUpdates = buildDuplexUpdates({ ...draft, api_key: ' k ' });
  assert.deepEqual(jevUpdates, {
    duplex_router_mode: 'active',
    duplex_router_enabled: 'true',
    duplex_router_provider: 'typed_choice',
    duplex_router_timeout_seconds: '2',
    duplex_router_typed_preset: 'jev',
    duplex_router_typed_model: 'jev-1.13.0',
    duplex_router_typed_endpoint: 'https://api.typesafe.ai/v1/systemone',
    duplex_router_api_key: 'k',
  });

  draft = selectDuplexPreset(draft, 'clef', saved);
  assert.equal(missingDuplexField(draft), 'account_id');
  const clefUpdates = buildDuplexUpdates({ ...draft, account_id: 'acct' });
  assert.equal(clefUpdates.duplex_router_typed_model, 'clef');
  assert.equal(clefUpdates.duplex_router_account_id, 'acct');
  assert.equal('duplex_router_typed_endpoint' in clefUpdates, false);

  draft = selectDuplexProvider(draft, 'logprob_llm', saved);
  draft = selectDuplexPreset(draft, 'openai', saved);
  const llmUpdates = buildDuplexUpdates(draft);
  assert.equal(llmUpdates.duplex_router_llm_api_base, 'https://api.openai.com/v1');
  assert.equal('duplex_router_api_key' in llmUpdates, false);
  assert.equal('duplex_router_typed_preset' in llmUpdates, false);

  draft = selectDuplexPreset(draft, 'custom', saved);
  assert.equal(missingDuplexField(draft), 'api_base');

  for (const updates of [jevUpdates, clefUpdates, llmUpdates, buildDuplexClearUpdates()]) {
    assert.doesNotThrow(() => normalizeSettingsConfigUpdates(updates));
  }
});

test('clear disables the supervisor and clears shared secrets', () => {
  const updates = buildDuplexClearUpdates();
  assert.equal(updates.duplex_router_enabled, 'false');
  assert.equal(updates.duplex_router_mode, 'off');
  assert.equal(updates.duplex_router_api_key, '');
  assert.equal(updates.duplex_router_account_id, '');
});
