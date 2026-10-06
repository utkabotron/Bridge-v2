"""Code that processor, analytics and bot must agree on, kept in exactly one place.

The services are separate Docker images and could not import each other, so the model
price table, the reasoning-model rules, the script regexes, the chat-context block of the
translation prompt, HTML escaping and ADMIN_TG_IDS parsing were copied between them — and
drifted: three price tables with three different sets of models, a bake-off whose
"mirror" of the translator's chat context could fall out of step with the real one, an
`esc` in analytics that did not escape quotes.

Modules:
  llm            prices, reasoning/Flex prefixes, request shape, token_cost
  scripts        Hebrew/Cyrillic/Latin/source-script regexes, target language → script
  chat_context   format_chat_context — the glossary/members/tone block of the prompt
  telegram_html  esc for parse_mode=HTML
  env            parse_ids, admin_tg_ids

How it reaches each service:
  images  docker-compose builds processor, analytics and bot with the repo root as the
          build context (`build: {context: ., dockerfile: <svc>/Dockerfile}`), and each
          Dockerfile copies shared/bridge_shared/ to /app/bridge_shared/, next to src/ (or
          flows/). /app is the working directory, so whatever makes the service's own code
          importable makes this package importable too. The root .dockerignore keeps that
          context down to these directories.
  tests   each service's tests/conftest.py puts shared/ on sys.path, so pytest works from
          the service directory and from the repo root (CI) alike.
  wa-service is Node and does not use it.

Rules: standard library only — no Prefect, FastAPI, OpenAI SDK or psycopg imports, each
image must import it with nothing but its own requirements; no I/O at import time;
nothing that only one service needs. A change here rebuilds all three images, so deploy
them together. processor/tests/test_shared.py fails if one of these definitions is copied
back into a service.
"""
