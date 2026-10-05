import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';
import { AgentMediaSettings, AgentSearchSettings, VideoGenSettings, VisualGenSettings } from './AgentSettings';

export const agentModule: SettingsModuleDefinition = {
  id: 'agent',
  titleKey: 'settingsPanel.categories.agent',
  icon: settingsNavigationIcons.agent,
  source: 'config',
  sections: [
    {
      id: 'skills',
      titleKey: 'settingsPanel.agent.skills',
      items: [
        { id: 'skill-evolution', component: 'switch', key: 'skill_evolution' },
        { id: 'ttse-enabled', component: 'switch', key: 'ttse_enabled' },
        {
          id: 'skill-retrieval',
          component: 'switch',
          key: 'skill_retrieval_enabled',
        },
      ],
    },
    {
      id: 'web-search',
      titleKey: 'settingsPanel.agent.webSearch',
      items: [
        { id: 'duckduckgo-search', component: 'switch', key: 'free_search_ddg_enabled' },
        { id: 'bing-search', component: 'switch', key: 'free_search_bing_enabled' },
        { id: 'search-credentials', component: 'custom', render: AgentSearchSettings },
      ],
    },
    {
      id: 'media-tools',
      titleKey: 'settingsPanel.agent.mediaTools',
      items: [
        { id: 'media-tools-settings', component: 'custom', render: AgentMediaSettings },
        { id: 'video-gen-settings', component: 'custom', render: VideoGenSettings },
        { id: 'visual-gen-settings', component: 'custom', render: VisualGenSettings },
      ],
    },
    {
      id: 'a2a-duplex-router',
      titleKey: 'settingsPanel.agent.a2aDuplexRouter',
      items: [
        {
          id: 'duplex-router-mode',
          component: 'select',
          key: 'duplex_router_mode',
          options: [
            { value: 'off', labelKey: 'settingsPanel.options.duplexRouterModeOff' },
            { value: 'shadow', labelKey: 'settingsPanel.options.duplexRouterModeShadow' },
            { value: 'active', labelKey: 'settingsPanel.options.duplexRouterModeActive' },
          ],
        },
        {
          id: 'duplex-router-backend',
          component: 'select',
          key: 'duplex_router_backend',
          options: [
            { value: 'sdk', labelKey: 'settingsPanel.options.duplexRouterBackendSdk' },
            { value: 'jev', labelKey: 'settingsPanel.options.duplexRouterBackendJev' },
            { value: 'mindshub', labelKey: 'settingsPanel.options.duplexRouterBackendMindshub' },
            { value: 'clef', labelKey: 'settingsPanel.options.duplexRouterBackendClef' },
          ],
        },
        {
          id: 'duplex-router-policy',
          component: 'select',
          key: 'duplex_router_policy',
          options: [
            { value: 'model', labelKey: 'settingsPanel.options.duplexRouterPolicyModel' },
            { value: 'always_interrupt', labelKey: 'settingsPanel.options.duplexRouterPolicyAlwaysInterrupt' },
            { value: 'steer', labelKey: 'settingsPanel.options.duplexRouterPolicySteer' },
            { value: 'serial', labelKey: 'settingsPanel.options.duplexRouterPolicySerial' },
          ],
        },
        { id: 'duplex-router-model-name', component: 'input', key: 'duplex_router_model_name' },
        { id: 'duplex-router-timeout', component: 'input', key: 'duplex_router_timeout_seconds' },
        { id: 'duplex-router-threshold', component: 'input', key: 'duplex_router_interrupt_threshold' },
        { id: 'duplex-router-api-base', component: 'input', key: 'duplex_router_api_base' },
        { id: 'duplex-router-endpoint', component: 'input', key: 'duplex_router_endpoint_path' },
        { id: 'duplex-router-api-key-env', component: 'input', key: 'duplex_router_api_key_env' },
        { id: 'duplex-router-account-env', component: 'input', key: 'duplex_router_account_id_env' },
        { id: 'duplex-router-clef-model', component: 'input', key: 'duplex_router_model' },
      ],
    },
  ],
};
