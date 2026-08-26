\set ON_ERROR_STOP on
\if :{?LEGACY_OWNER}
\else
  \echo '缺少 LEGACY_OWNER；使用 -v LEGACY_OWNER=legacy-alice'
  \quit 2
\endif
\if :{?TARGET_OWNER}
\else
  \echo '缺少 TARGET_OWNER；使用 -v TARGET_OWNER=auth:42'
  \quit 2
\endif

BEGIN TRANSACTION READ ONLY;

SELECT :'LEGACY_OWNER' AS legacy_owner,
       :'TARGET_OWNER' AS target_owner,
       (SELECT count(*) FROM conversations
         WHERE owner_id = :'LEGACY_OWNER') AS conversation_candidates,
       (SELECT count(*) FROM agent_traces
         WHERE user_id = :'LEGACY_OWNER') AS trace_candidates,
       ((SELECT count(*) FROM conversations
          WHERE owner_id = :'LEGACY_OWNER')
        +
        (SELECT count(*) FROM agent_traces
          WHERE user_id = :'LEGACY_OWNER')) AS candidate_count;

SELECT owner_id, count(*) AS rows
FROM conversations
WHERE owner_id IN (:'LEGACY_OWNER', :'TARGET_OWNER')
GROUP BY owner_id
ORDER BY owner_id;

SELECT user_id, count(*) AS rows
FROM agent_traces
WHERE user_id IN (:'LEGACY_OWNER', :'TARGET_OWNER')
GROUP BY user_id
ORDER BY user_id;

ROLLBACK;
