PY ?= .venv/bin/python

venv:          ## 建本地虚拟环境（Python 3.13）
	python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt

up:            ## 起数据库与 web（Docker）
	docker compose up -d

migrate:       ## 建表
	$(PY) manage.py migrate

test:          ## 跑测试（纯函数测试无需 API key）
	$(PY) manage.py test research -v 2

doctor:        ## 环境体检：一条命令确认整个栈就绪（零 API 成本）
	$(PY) manage.py doctor

ingest:        ## 完整摄取（约 1 小时 / ~$$23.5，需 OPENAI_API_KEY）
	$(PY) manage.py ingest --resume

render:        ## 只补渲页面 PNG（loaddata 之后用，零 API 调用）
	$(PY) manage.py ingest --render-only

demo:          ## clone 后最短路径：数据库 + 迁移 + fixture + 重渲 PNG + 起服务
	@test -f fixtures/corpus.json.gz || \
		(echo "fixture 随交付包提供（内含授权内容，不入公开仓库）——或用 make ingest 自建"; exit 1)
	docker compose up -d db
	$(PY) manage.py migrate
	$(PY) manage.py loaddata fixtures/corpus.json.gz
	$(PY) manage.py ingest --render-only
	$(PY) manage.py runserver

.PHONY: venv up migrate test doctor ingest render demo
