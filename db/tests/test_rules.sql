-- Business-rule tests run against a fresh crm + automation database.
-- Each block must either succeed or raise the expected error; any other
-- outcome aborts the script (ON_ERROR_STOP) and fails the test run.
\set ON_ERROR_STOP 1
\c crm
SET search_path = crm, public;

CREATE TEMP TABLE t AS SELECT
  gen_random_uuid() AS md, gen_random_uuid() AS mgr, gen_random_uuid() AS contact,
  gen_random_uuid() AS deal, gen_random_uuid() AS quote, gen_random_uuid() AS v1,
  gen_random_uuid() AS appr, gen_random_uuid() AS grant_id, gen_random_uuid() AS msg1,
  gen_random_uuid() AS msg2, repeat('a',64)::char(64) AS sha, repeat('b',64)::char(64) AS rcpt;

INSERT INTO users (id, email, full_name, role) SELECT md,  'md@test.ae',  'MD',      'md'      FROM t;
INSERT INTO users (id, email, full_name, role) SELECT mgr, 'mgr@test.ae', 'Manager', 'manager' FROM t;
INSERT INTO contacts (id, full_name, phone_e164, source) SELECT contact, 'Ahmed', '+971501234567', 'web_chat' FROM t;
INSERT INTO deals (id, primary_contact_id, source) SELECT deal, contact, 'web_chat' FROM t;
INSERT INTO quotes (id, deal_id) SELECT quote, deal FROM t;
INSERT INTO quote_versions (id, quote_id, version_no, kind, currency, subtotal, total, total_aed,
                            validity_days, created_by_type, created_by)
  SELECT v1, quote, 1, 'initial', 'AED', 1000, 1050, 1050, 7, 'ai', 'quotation' FROM t;
INSERT INTO quote_lines (quote_version_id, line_no, description, quantity, unit_price, line_total)
  SELECT v1, 1, 'Al container 750ml', 50000, 0.02, 1000 FROM t;

-- T1: duplicate phone must be rejected (one person = one record)
DO $$ BEGIN
  INSERT INTO crm.contacts (full_name, phone_e164, source) VALUES ('Dup', '+971501234567', 'manual');
  RAISE EXCEPTION 'FAIL T1: duplicate phone accepted';
EXCEPTION WHEN unique_violation THEN RAISE NOTICE 'PASS T1 duplicate phone rejected'; END $$;

-- submit for approval: version leaves draft
UPDATE quote_versions SET status = 'pending_approval', content_sha256 = (SELECT sha FROM t) WHERE id = (SELECT v1 FROM t);

-- T2: frozen price
DO $$ BEGIN
  UPDATE crm.quote_versions SET total = 900 WHERE id = (SELECT v1 FROM t);
  RAISE EXCEPTION 'FAIL T2: frozen total changed';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T2 frozen version price cannot change'; END $$;

-- T3: lines frozen
DO $$ BEGIN
  INSERT INTO crm.quote_lines (quote_version_id, line_no, description, quantity, unit_price, line_total)
  SELECT v1, 2, 'extra', 1, 1, 1 FROM t;
  RAISE EXCEPTION 'FAIL T3: line added to frozen version';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T3 frozen version lines cannot change'; END $$;

-- T4: final quote may only be approved by the MD (T2 decision)
DO $$ BEGIN
  INSERT INTO crm.approvals (quote_version_id, kind, required_role, window_minutes, deadline_at)
  SELECT v1, 'final', 'manager', 120, now() + interval '2 hours' FROM t;
  RAISE EXCEPTION 'FAIL T4: manager allowed to confirm final quote';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T4 final quote requires MD'; END $$;

INSERT INTO approvals (id, quote_version_id, kind, required_role, assigned_user_id, window_minutes, deadline_at)
  SELECT appr, v1, 'initial', 'manager', mgr, 120, now() + interval '2 hours' FROM t;

-- T5: only one open approval per version
DO $$ BEGIN
  INSERT INTO crm.approvals (quote_version_id, kind, required_role, window_minutes, deadline_at)
  SELECT v1, 'initial', 'manager', 120, now() + interval '2 hours' FROM t;
  RAISE EXCEPTION 'FAIL T5: second open approval accepted';
EXCEPTION WHEN unique_violation THEN RAISE NOTICE 'PASS T5 one open approval per version'; END $$;

-- T6: approving needs a decider
DO $$ BEGIN
  UPDATE crm.approvals SET status = 'approved' WHERE id = (SELECT appr FROM t);
  RAISE EXCEPTION 'FAIL T6: approval without decider';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T6 approval records who decided'; END $$;

UPDATE approvals SET status = 'approved', decided_by = (SELECT mgr FROM t), decided_at = now() WHERE id = (SELECT appr FROM t);
UPDATE quote_versions SET status = 'approved' WHERE id = (SELECT v1 FROM t);
INSERT INTO quote_send_grants (id, quote_version_id, approval_id, policy, content_sha256, channel, recipient_hash, expires_at)
  SELECT grant_id, v1, appr, 'manager', sha, 'whatsapp', rcpt, now() + interval '24 hours' FROM t;

-- T7..T11: send-grant semantics
DO $$ DECLARE r record; ok boolean; BEGIN
  SELECT * INTO r FROM t;
  ok := crm.consume_send_grant(r.grant_id, repeat('c',64)::char(64), r.msg1, r.rcpt);
  IF ok THEN RAISE EXCEPTION 'FAIL T7: grant consumed with wrong content hash'; END IF;
  RAISE NOTICE 'PASS T7 wrong content hash refused';
  ok := crm.consume_send_grant(r.grant_id, r.sha, r.msg1, repeat('d',64)::char(64));
  IF ok THEN RAISE EXCEPTION 'FAIL T8: grant consumed for another recipient'; END IF;
  RAISE NOTICE 'PASS T8 wrong recipient refused';
  ok := crm.consume_send_grant(r.grant_id, r.sha, r.msg1, r.rcpt);
  IF NOT ok THEN RAISE EXCEPTION 'FAIL T9: valid grant refused'; END IF;
  RAISE NOTICE 'PASS T9 valid grant consumed';
  ok := crm.consume_send_grant(r.grant_id, r.sha, r.msg1, r.rcpt);
  IF NOT ok THEN RAISE EXCEPTION 'FAIL T10: retry of same message refused'; END IF;
  RAISE NOTICE 'PASS T10 retry of the same message is idempotent';
  ok := crm.consume_send_grant(r.grant_id, r.sha, r.msg2, r.rcpt);
  IF ok THEN RAISE EXCEPTION 'FAIL T11: grant reused by a second message'; END IF;
  RAISE NOTICE 'PASS T11 grant cannot be reused by another message';
END $$;

-- T12: revoked / expired grants refuse
DO $$ DECLARE r record; g2 uuid := gen_random_uuid(); ok boolean; BEGIN
  SELECT * INTO r FROM t;
  INSERT INTO crm.quote_send_grants (id, quote_version_id, approval_id, policy, content_sha256, channel, recipient_hash, issued_at, expires_at)
  VALUES (g2, r.v1, r.appr, 'manager', r.sha, 'email', r.rcpt, now() - interval '2 days', now() - interval '1 day');
  ok := crm.consume_send_grant(g2, r.sha, gen_random_uuid(), r.rcpt);
  IF ok THEN RAISE EXCEPTION 'FAIL T12: expired grant consumed'; END IF;
  RAISE NOTICE 'PASS T12 expired grant refused';
END $$;

-- T13: LOST needs a reason
DO $$ BEGIN
  UPDATE crm.deals SET stage = 'LOST' WHERE id = (SELECT deal FROM t);
  RAISE EXCEPTION 'FAIL T13: LOST without reason';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T13 LOST requires a reason'; END $$;

-- T14: AI never trades longer delivery for price (T5 decision)
DO $$ BEGIN
  INSERT INTO crm.negotiation_rounds (deal_id, quote_version_id, round_no, channel, verdict, concession_no, traded_for)
  SELECT deal, v1, 1, 'whatsapp', 'concession', 1, 'longer_delivery' FROM t;
  RAISE EXCEPTION 'FAIL T14: longer delivery accepted as concession';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T14 longer delivery never a concession'; END $$;

-- T15: audit log is append-only
INSERT INTO audit_events (actor_type, actor_id, action, entity_type, entity_id) VALUES ('system','test','x','deal','1');
DO $$ BEGIN
  DELETE FROM crm.audit_events;
  RAISE EXCEPTION 'FAIL T15: audit row deleted';
EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS T15 audit log is append-only'; END $$;

-- T16: margin range must be ordered
DO $$ BEGIN
  INSERT INTO crm.product_categories VALUES ('ALU', 'Aluminium containers') ON CONFLICT DO NOTHING;
  INSERT INTO crm.margin_rules (category_code, markup_min_pct, markup_max_pct) VALUES ('ALU', 50, 40);
  RAISE EXCEPTION 'FAIL T16: inverted markup range accepted';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T16 markup range validated'; END $$;

-- T17: an approved material rate must name who approved it
DO $$ BEGIN
  INSERT INTO crm.materials VALUES ('AL_COIL_8011','Aluminium coil 8011','kg','AED') ON CONFLICT DO NOTHING;
  INSERT INTO crm.material_rates (material_code, rate_per_unit, source, status) VALUES ('AL_COIL_8011', 11.2, 'scrape', 'approved');
  RAISE EXCEPTION 'FAIL T17: scraped rate approved without a manager';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T17 scraped rate needs manager approval'; END $$;

\c automation
SET search_path = automation, public;
INSERT INTO channel_accounts (id, channel, provider, external_account_id, display_name, address, purpose, secret_ref)
VALUES ('00000000-0000-0000-0000-000000000001', 'whatsapp', 'meta_cloud', 'PNID1', 'Arifaa Pack', '+97140000000', 'customer', 'arn:test');

-- T18: a priced message cannot exist without a grant
DO $$ BEGIN
  INSERT INTO automation.outbound_messages (channel_account_id, to_address, kind, body, requested_by, idempotency_key)
  VALUES ('00000000-0000-0000-0000-000000000001', '+971501234567', 'quote', 'price ...', 'communication', 'k1');
  RAISE EXCEPTION 'FAIL T18: quote message without grant';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T18 quote message requires a grant'; END $$;

-- T19: one message per grant
INSERT INTO outbound_messages (channel_account_id, to_address, kind, grant_ref, content_sha256, requested_by, idempotency_key)
VALUES ('00000000-0000-0000-0000-000000000001', '+971501234567', 'quote', '00000000-0000-0000-0000-0000000000aa', repeat('a',64), 'communication', 'k2');
DO $$ BEGIN
  INSERT INTO automation.outbound_messages (channel_account_id, to_address, kind, grant_ref, content_sha256, requested_by, idempotency_key)
  VALUES ('00000000-0000-0000-0000-000000000001', '+971501234567', 'quote', '00000000-0000-0000-0000-0000000000aa', repeat('a',64), 'communication', 'k3');
  RAISE EXCEPTION 'FAIL T19: grant used by two messages';
EXCEPTION WHEN unique_violation THEN RAISE NOTICE 'PASS T19 one message per grant'; END $$;

-- T20: webhook replays are de-duplicated
INSERT INTO webhook_receipts (provider, external_id, signature_ok) VALUES ('meta', 'wamid.1', true);
DO $$ BEGIN
  INSERT INTO automation.webhook_receipts (provider, external_id, signature_ok) VALUES ('meta', 'wamid.1', true);
  RAISE EXCEPTION 'FAIL T20: webhook replay accepted';
EXCEPTION WHEN unique_violation THEN RAISE NOTICE 'PASS T20 webhook replay de-duplicated'; END $$;

\c agents
SET search_path = agents, public;
-- T21: vector search works (hybrid index sanity)
INSERT INTO kb_documents (id, cms_collection, cms_id, cms_version, title, content_sha256, approved_at)
VALUES ('00000000-0000-0000-0000-0000000000d1', 'faqs', '1', '1', 'MOQ', repeat('e',64), now());
INSERT INTO kb_chunks (document_id, chunk_no, text, embedding, embedding_model)
SELECT '00000000-0000-0000-0000-0000000000d1', 1, 'Minimum order quantity for 750 ml containers is 10,000 pieces',
       array_fill(0.01::real, ARRAY[1024])::vector, 'test';
DO $$ DECLARE n int; BEGIN
  SELECT count(*) INTO n FROM agents.kb_chunks
   WHERE tsv @@ plainto_tsquery('simple', 'minimum order')
   ORDER BY 1 LIMIT 1;
  IF n <> 1 THEN RAISE EXCEPTION 'FAIL T21: keyword search'; END IF;
  PERFORM 1 FROM agents.kb_chunks ORDER BY embedding <=> array_fill(0.01::real, ARRAY[1024])::vector LIMIT 1;
  RAISE NOTICE 'PASS T21 keyword + vector search';
END $$;
