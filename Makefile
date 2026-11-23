.PHONY: start up down restart logs ps test e2e fake-lm clean

start:         ## sobe RAG + bridge/túnel e valida saúde
	./start.sh

up:            ## sobe somente a stack Docker (build se necessário)
	docker compose up -d --build

down:          ## para os containers (dados preservados)
	docker compose down

restart:       ## reinicia os containers (dados preservados)
	docker compose restart

logs:          ## acompanha logs do app
	docker compose logs -f app

ps:
	docker compose ps

test:          ## testes unitários/integração (requer stack no ar; usa DB de teste)
	docker compose exec app python -m pytest /srv/rag/tests -x -q || \
	python3 -m pytest tests -x -q

e2e:           ## validação end-to-end completa contra a stack no ar
	python3 scripts/e2e_check.py --url http://localhost:$${RAG_PORT:-8000}

fake-lm:       ## LM Studio simulado local (apenas para testes sem LM Studio real)
	python3 scripts/fake_lmstudio.py --port 1234 --host 0.0.0.0

clean:         ## CUIDADO: apaga containers E volumes (dados!)
	docker compose down -v
