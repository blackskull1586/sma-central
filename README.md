# SMA Central

A Central reúne servidores lógicos de videomonitoramento em uma aplicação web. O proprietário cria servidores, usuários e canais. Os transmissores enviam vídeo por RTMP. Os espectadores autorizados assistem por WebRTC ou HLS e o proprietário pode baixar as gravações em MP4. Cada instalação tem seu próprio banco, acervo e credenciais.

## Instalação com wget

Execute no terminal da VPS ou do computador que hospedará a Central na rede local (LAN). Use Ubuntu Server 24.04 ou 26.04 x86-64 e um endereço estável. Na LAN, pode ser um IPv4 fixo ou uma reserva de DHCP no roteador. A primeira instalação precisa de internet para baixar o código, as dependências e as imagens Docker.

```bash
wget -O setup.sh https://raw.githubusercontent.com/blackskull1586/sma-central/main/setup.sh
sudo bash setup.sh
```

O instalador verifica os arquivos e instala a Central em `/opt/SMA_Central`. Em uma VPS com IPv4 público atribuído à interface de saída, usa esse IP. Em uma LAN, identifica o IPv4 privado da máquina. Ao terminar, confere o certificado e o login e mostra o endereço HTTPS que os usuários devem abrir.

Na LAN, os clientes acessam o IP privado diretamente. Se houver mais de um endereço possível, ou se quiser escolher outra interface, informe o IPv4 desejado, por exemplo `sudo bash setup.sh 192.168.1.50`. Para oferecer acesso pela internet a uma máquina atrás de NAT, informe o IPv4 público encaminhado pelo roteador.

| Porta de destino | Uso |
| --- | --- |
| TCP 80 e 443 | Emissão e renovação do certificado e acesso à Central |
| TCP 19350 | Recepção RTMP dos transmissores |
| UDP e TCP 28080 | Mídia WebRTC dos espectadores |

Para uso exclusivo na LAN, permita essas portas no firewall do servidor para os dispositivos autorizados da rede. O encaminhamento de portas no roteador só se aplica quando o acesso chega de fora por NAT. O RTMP padrão envia a chave e a mídia sem criptografia de transporte. Para redes restritas, use uma VPN ou limite as origens do envio.

## Primeiro acesso

**HTTPS na LAN.** Com um IPv4 privado, a Central gera o certificado HTTPS localmente. Copie o certificado público `/opt/SMA_Central/SMA_CA_Local.crt` para cada dispositivo cliente, confira a impressão SHA-256 mostrada no terminal do servidor e importe o certificado como autoridade confiável. Abra o endereço exibido pelo instalador, por exemplo `https://192.168.1.50/`. Para um domínio com certificado público, as validações externas precisam alcançar a máquina.

1. Abra o endereço HTTPS exibido no terminal. O usuário inicial é **central**. Consulte a senha provisória no terminal privado com `sudo less /opt/SMA_Central/runtime/bootstrap.json`. Troque-a no primeiro login.
2. Abra **Servidores → Novo servidor**, informe nome, identificador e credenciais provisórias do administrador. A criação é imediata. Cada servidor lógico separa contas e canais e compartilha os recursos físicos da máquina.
3. Na lista, escolha **Administrar** para criar ambientes, usuários e canais. Copie a URL RTMP e a chave do canal para o transmissor. Use **Gravações** na Central para consultar e baixar os arquivos após o encerramento da transmissão.
4. Para abrir a Central em janela própria em dispositivos compatíveis, use o comando de instalação do aplicativo no navegador. No Safari do iPhone/iPad, use **Compartilhar → Adicionar à Tela de Início**.

## Usar um domínio opcional

1. No provedor do seu domínio, crie um registro **A** para o IPv4 da VPS, por exemplo `central.seudominio.com.br`. Se publicar também **AAAA**, ele deve apontar para o IPv6 reconhecido pela instalação. Durante a configuração, mantenha os registros apontados diretamente à VPS.
2. Entre na Central pelo IP com a conta proprietária. Abra **Domínios**, informe o nome completo e salve. O painel mostra o resultado da verificação DNS e o estado do HTTPS.
3. Quando o domínio estiver ativo, abra `https://central.seudominio.com.br/` e autentique-se no novo endereço. O mesmo IP continua disponível. A aba permite desativar o domínio ou solicitar nova verificação depois de corrigir o DNS.

## Verificar e manter

```bash
cd /opt/SMA_Central
sudo docker compose ps -a
sudo python3 deploy/verificar.py
```

Para examinar uma falha, consulte `sudo docker compose logs --tail=60` seguido do serviço indicado no verificador. Em caso de erro na mesma versão, corrija a causa e execute `sudo bash setup.sh` na pasta do instalador. Se a configuração já tiver sido criada, o SMA reutiliza o endereço salvo. Preserve o banco, os volumes do Caddy e a pasta `runtime/` em backups protegidos. A política inicial de gravação é 60 GiB de acervo e reserva de 20 GiB de espaço livre. Dimensione disco e link de acordo com as transmissões simultâneas.

O roteiro em [testes/LEIA-ME.md](testes/LEIA-ME.md) verifica criação e isolamento de servidores, transmissão e download com uma fonte sintética. Os créditos e avisos de terceiros estão em [TERCEIROS.md](TERCEIROS.md).
