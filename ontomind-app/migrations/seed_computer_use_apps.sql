-- COMPUTER_USE 类型应用种子（生产 agent_app 表实录还原）
-- 注意：type CHECK 约束需先执行 V56__extend_for_manifests.sql 放开

INSERT INTO agent_app (id, tenant_id, name, type, version, status, model_key, knowledge_base_ids_json, system_prompt, retrieval_config_json)
VALUES
  ('app-computer-use', 'default', 'Computer Use Agent', 'COMPUTER_USE', 1, 'ACTIVE', NULL, '[]',
   '你是 Computer Use Agent，通过 browser 工具控制浏览器操作网页。Chrome 浏览器已通过 CDP 远程调试协议连接，无需确认连接状态，直接使用 browser_navigate 等工具操作即可。你可以导航到指定URL、点击元素、填写表单、提取页面内容。始终先调用 browser_snapshot 观察页面状态再决定下一步操作，遇到验证码或需要人工干预时暂停并告知用户。当常规点击不生效时可用 browser_eval 提交表单。',
   '{}')
ON CONFLICT (id) DO NOTHING;
