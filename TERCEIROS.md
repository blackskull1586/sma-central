# Componentes de terceiros e imagem da interface

A fotografia ilustrativa da onça e os recortes exibidos no login, nas telas sem vídeo e nos ícones foram gerados para esta distribuição pública. O SMA conserva o nome e os créditos definidos pelos responsáveis pelo projeto.

| Componente distribuído na interface | Versão | Licença e arquivos incluídos |
| --- | --- | --- |
| hls.js | 1.5.15 | Apache 2.0. Os avisos de Dailymotion e Brightcove estão em `web/js/LICENSE-hls.js.txt`. O texto completo está em `web/Apache-2.0.txt`. |
| SortableJS | 1.15.2 | MIT. O aviso de copyright e o texto integral estão em `web/js/LICENSE-sortablejs.txt`. |

O instalador baixa as dependências Python durante o build. Os arquivos `vendor/portal/requirements.lock` e `vendor/gateway/requirements.lock` identificam versões e hashes aceitos. Os pacotes de origem incluem as licenças e os avisos de seus autores. As imagens base do Python, PostgreSQL, Debian, Caddy e SRS são obtidas dos registros durante a instalação. As dependências do sistema, incluindo NGINX, o módulo RTMP e FFmpeg, vêm do gerenciador de pacotes da distribuição da respectiva imagem.

Projetos da interface: [hls.js](https://github.com/video-dev/hls.js/tree/v1.5.15) e [SortableJS](https://github.com/SortableJS/Sortable/tree/1.15.2).
