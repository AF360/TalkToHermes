# syntax=docker/dockerfile:1
# Reviewed immutable Caddy 2.11.4 bases and Apache-2.0 Cloudflare DNS module.
FROM caddy:2.11.4-builder@sha256:4bdeabce8e79d36b23d1cba7d20598cec2c1117ace960d8ca06071f945e8fc9b AS builder
RUN xcaddy build v2.11.4 \
      --with github.com/caddy-dns/cloudflare@a8737d095ad5a48ca031cea6ab704057dbc2d250

FROM caddy:2.11.4@sha256:df7f1c2fb114453b951de51a98efc010db1655a92c2e86be6706714e2417a78d
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
