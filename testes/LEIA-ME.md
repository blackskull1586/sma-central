# Validação do SMA em IP único

Execute na VPS da instalação nova com Python 3 e acesso ao projeto Docker Compose. Informe o IPv4 que consta no certificado. O teste usa a interface HTTPS apresentada nesse IP e uma fonte audiovisual sintética. Ele leva cerca de dois minutos sem receptor externo e até quatro minutos com receptor.

```bash
cd /opt/SMA_Central/testes
sudo python3 teste_sma_ip.py --compose-dir /opt/SMA_Central --ip IPV4_DA_NOVA_VPS
```

O teste cria um proprietário temporário no banco da instalação. Pela interface web, ele cria dois servidores lógicos, os administradores, os canais e um visualizador. Confere a troca das senhas, o isolamento entre os servidores e o bloqueio do acervo para as contas regionais. Publica um clipe H.264/AAC pela porta RTMP exposta, lê HLS pelo HTTPS, negocia WebRTC e verifica o download da gravação finalizada na Central. A conexão HTTPS local faz a verificação TLS usando o próprio IP. Para uma CA privada, indique o PEM confiável com `--ca /caminho/ca.pem`.

Para verificar a decodificação WebRTC pela internet, execute com `--wan-handoff /diretorio-privado/acesso-rtc.json`. O programa criará um arquivo temporário com modo 0600 e removerá o arquivo ao encerrar. Enquanto o clipe estiver ativo, entregue o conteúdo ao receptor pela entrada padrão através de uma conexão autenticada. A conta é de visualização e pertence ao servidor de teste. A saída do receptor informa os quadros decodificados e os pacotes RTP recebidos.

Em uma máquina externa autorizada com Docker, prepare o receptor e confirme a porta ICE publicada no projeto. O exemplo usa a porta 28080.

```bash
docker build -f Dockerfile.rtc-receiver -t sma-qa-rtc .
ssh usuario@vps 'sudo cat /diretorio-privado/acesso-rtc.json' | docker run --rm -i sma-qa-rtc --host IPV4_DA_NOVA_VPS --candidate-port 28080
```

Se o certificado do IP usar CA privada, monte o PEM público da CA em modo leitura no receptor e acrescente `--ca /ca.pem`. O recebimento de três quadros H.264 de 320 × 180 e de pacotes RTP confirma a passagem de vídeo no caminho testado. A negociação SDP isoladamente confirma a sinalização.

O teste encerra as sessões e remove somente os registros que reconhece como pertencentes à sua execução. Havendo exportação pendente, ele desativa o ambiente de teste, preserva a gravação e informa o identificador. Para repetir a limpeza após a finalização, use o mesmo diretório e IP, com `--cleanup ID_DE_EXECUCAO`. O acervo aplica sua política normal de exclusão às gravações sintéticas.
