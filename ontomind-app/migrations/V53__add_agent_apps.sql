-- OntoMind V53 摘录：agent_app 表（自研，来源见 NOTICE）
CREATE TABLE IF NOT EXISTS agent_app (
    id VARCHAR(100) PRIMARY KEY,
    tenant_id VARCHAR(100) NOT NULL DEFAULT 'default',
    name VARCHAR(200) NOT NULL,
    type VARCHAR(50) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(30) NOT NULL DEFAULT 'DRAFT',
    model_key VARCHAR(300),
    knowledge_base_ids_json TEXT NOT NULL DEFAULT '[]',
    system_prompt TEXT,
    retrieval_config_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_app_tenant_status
    ON agent_app(tenant_id, status, updated_at DESC);
