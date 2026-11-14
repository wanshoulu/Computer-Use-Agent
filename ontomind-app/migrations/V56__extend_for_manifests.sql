-- OntoMind V56 摘录：放开 agent_app.type CHECK（使 COMPUTER_USE 等新类型可注册）+ 知识清单关联
ALTER TABLE agent_app DROP CONSTRAINT IF EXISTS ck_agent_app_type;

ALTER TABLE agent_app
    ADD COLUMN IF NOT EXISTS current_knowledge_manifest_id VARCHAR(100),
    ADD COLUMN IF NOT EXISTS legacy_knowledge_migrated_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_agent_app_tenant_manifest
    ON agent_app(tenant_id, current_knowledge_manifest_id);
