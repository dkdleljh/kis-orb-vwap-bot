# Changelog

## [0.2.0](https://github.com/dkdleljh/kis-orb-vwap-bot/compare/v0.1.0...v0.2.0) (2026-02-19)


### Features

* add cash reserve + entry rate limit guardrails ([65e4b3b](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/65e4b3b4242a9eb956a86b4e564cff3ec570b0f8))
* add module tagging + signal reasons for explainable reports ([b49e9c3](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/b49e9c382db7e6169efa250b2a8e95e1850a5133))
* add multi-position live trading support with max concurrent limit ([fb9f2d9](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/fb9f2d9c1c81fafe78c6e7205352243f498452d2))
* add overseas open orders inquiry (inquire-nccs) ([0b24dfa](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/0b24dfafbe64b8a4304155a72e255eb22a65f794))
* add per-symbol exposure cap (max_symbol_position_pct) ([ffdd9c7](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/ffdd9c7ec9d711d2bd3778234b8ecd981ca217dd))
* add portfolio exposure cap (max_total_position_pct) ([c5aa026](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/c5aa0260186cf26e99a7d38e872fcd2dda78dbb5))
* add US reserved order list inquiry (order-resv-list) ([a3fda78](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/a3fda7843fd9edc66dcd176786034fc137f37265))
* dynamic entry threshold + improved exits + reasons win/loss analysis ([711e281](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/711e28122019a21d43adf515b222ca3b1bd90969))
* enable US modules + add stop script ([862e19c](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/862e19cec16917a928605a8662492ab5f3844d6b))
* enrich daily report (risk reasons, slippage, source stats) ([a710892](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/a710892607862f55ffda66aeb07faa13ea7a0ebe))
* generate next-day tuning recommendations ([9afb115](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/9afb11576316beb3edc4e60cb6b9d0118c21f8f1))
* harden next-day prep diagnostics and reporting ([aa8e881](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/aa8e8815329e117965dfbc14e25417509a317f40))
* include daily return (baseline equity) in reports ([03fdd4a](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/03fdd4a2a3e0580af8c5f26080833910640fdfb9))
* reserved US order dedupe + cancel support ([dc39d4f](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/dc39d4fa6e7bd02a6118292945ccee6f9964ac1f))


### Bug Fixes

* make reserved-order prune idempotent ([875dcbb](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/875dcbbd56d004ad5d2efbf721363d76a9781efa))
* overseas sell market order type + ORD_DVSN field variants ([8c451c5](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/8c451c512828a18f2a5fd8c67ca65e76a080719c))
* prevent repeated SCHD reserved TP submissions ([16bc176](https://github.com/dkdleljh/kis-orb-vwap-bot/commit/16bc176b968a22a33c6f8d31d378cf077c5f569d))
