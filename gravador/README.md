# Gravador automático

O serviço copia segmentos concluídos do volume HLS, montado somente para leitura em `/hls`, e guarda o acervo privado em `/var/lib/sma-recordings`. Central e gravador devem usar UID/GID 10001 e compartilhar o mesmo acervo. O gravador usa o mesmo HLS das transmissões e começa a capturar no primeiro segmento anunciado.

A configuração incluída limita o acervo a **60 GiB** e preserva **20 GiB livres** no sistema de arquivos. A reserva inclui a estimativa de espaço de trabalho da conversão. O gravador suspende novas cópias quando atinge o limite e mantém os vídeos existentes. A recuperação de espaço ocorre pela exclusão autorizada no Central ou pela expansão do armazenamento. O limite é calculado pela aplicação, e outros serviços também podem consumir espaço no disco.

A gravação fecha após 15 segundos sem segmentos, em descontinuidades ou em partes de 15 minutos. O manifesto registra lacunas, início estimado, offset -04:00, canal e número da fonte. Uma chave compartilhada identifica o canal. O campo de operador individual permanece vazio enquanto o sistema não fornecer uma identidade autenticada do transmissor.

O arquivo de exemplo é incorporado à imagem como `/opt/recorder/config.json`. O gravador consulta somente as colunas listadas em `grant-metadata.sql`, com papel PostgreSQL próprio, SQL em transação somente leitura e conexão sem privilégio administrativo. O instalador fornece `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` e `SMA_PGPASSWORD_FILE`. A exportação usa FFmpeg e FFprobe no mesmo contêiner, sem acesso ao socket Docker. Quando o banco está indisponível e a fonte é desconhecida, os segmentos ficam preservados. A identificação e a geração do MP4 são retomadas após a recuperação dos metadados.


Com Python 3.12 e FFmpeg instalados, rode `python3 -m unittest discover -s . -v` a partir desta pasta. O ensaio de mídia gera dois fluxos H.264 em diretório temporário, um com áudio AAC e outro sem áudio, grava seus MP4 e os decodifica integralmente. Os dados e credenciais da operação não são necessários.