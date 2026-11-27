SHELL := /bin/bash
COMPOSE_FILE := deploy/docker-compose.yml

.PHONY: up down run test chaos bench lint

up:
	docker compose -f $(COMPOSE_FILE) up -d --wait

down:
	docker compose -f $(COMPOSE_FILE) down -v

lint:
	python3 -m py_compile controlplane/zone_lease.py controlplane/server.py dataplane/dns_failover.py storage/wal_chain.py tests/test_dns.py

run:
	AWS_ENDPOINT_URL=http://localhost:4566 \
	OPA_URL=http://localhost:8181 \
	DNS_STATE_BUCKET=global-dns-failover-state \
	WEBHOOK_HMAC_SECRET=local-dev-dns-secret \
	uvicorn controlplane.server:app --host 0.0.0.0 --port 8080

test:
	AWS_ENDPOINT_URL=http://localhost:4566 \
	OPA_URL=http://localhost:8181 \
	DNS_STATE_BUCKET=global-dns-failover-state \
	WEBHOOK_HMAC_SECRET=local-dev-dns-secret \
	python3 -m unittest discover -s tests -p "test_*.py" -v

chaos:
	python3 -m unittest tests/test_dns.py -k test_chaos_stalled_reconciler_stale_token_rejected -v

bench:
	python3 -m unittest tests/test_dns.py -k test_bench_failover_and_wal -v
