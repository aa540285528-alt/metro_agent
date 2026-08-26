\set ON_ERROR_STOP on
\if :{?LEGACY_OWNER}
\else
  \echo '缺少 LEGACY_OWNER'
  \quit 2
\endif
\if :{?TARGET_OWNER}
\else
  \echo '缺少 TARGET_OWNER'
  \quit 2
\endif
\if :{?EXPECTED_COUNT}
\else
  \echo '缺少人工审批的 EXPECTED_COUNT'
  \quit 2
\endif
\if :{?BACKUP_REFERENCE}
\else
  \echo '缺少已验证备份的 BACKUP_REFERENCE'
  \quit 2
\endif

SELECT :'TARGET_OWNER' ~ '^auth:[0-9]+$' AS target_owner_is_valid \gset
\if :target_owner_is_valid
\else
  \echo 'TARGET_OWNER 必须匹配 auth:<数字 id>'
  \quit 2
\endif

SELECT :'LEGACY_OWNER' <> :'TARGET_OWNER' AS owners_are_distinct \gset
\if :owners_are_distinct
\else
  \echo 'LEGACY_OWNER 与 TARGET_OWNER 不能相同'
  \quit 2
\endif

BEGIN;
LOCK TABLE conversations, agent_traces IN SHARE ROW EXCLUSIVE MODE;

SELECT ((SELECT count(*) FROM conversations
          WHERE owner_id = :'LEGACY_OWNER')
        +
        (SELECT count(*) FROM agent_traces
          WHERE user_id = :'LEGACY_OWNER'))::bigint AS candidate_count \gset

SELECT :candidate_count::bigint = :'EXPECTED_COUNT'::bigint AS candidate_count_matches \gset
\if :candidate_count_matches
\else
  \echo '候选数不等于 EXPECTED_COUNT；回滚并停止'
  ROLLBACK;
  \quit 3
\endif

WITH changed AS (
  UPDATE conversations
  SET owner_id = :'TARGET_OWNER'
  WHERE owner_id = :'LEGACY_OWNER'
  RETURNING 1
)
SELECT count(*)::bigint AS updated_conversations FROM changed \gset

WITH changed AS (
  UPDATE agent_traces
  SET user_id = :'TARGET_OWNER'
  WHERE user_id = :'LEGACY_OWNER'
  RETURNING 1
)
SELECT count(*)::bigint AS updated_traces FROM changed \gset

SELECT (:updated_conversations::bigint + :updated_traces::bigint) AS updated_count \gset
SELECT :updated_count::bigint = :'EXPECTED_COUNT'::bigint AS updated_count_matches \gset
\if :updated_count_matches
\else
  \echo '实际更新数不等于 EXPECTED_COUNT；回滚并停止'
  ROLLBACK;
  \quit 4
\endif

COMMIT;
