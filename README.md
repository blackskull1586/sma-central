# SMA Central

A Central reúne servidores lógicos de videomonitoramento em uma aplicação web. O proprietário cria servidores, usuários e canais. Os transmissores enviam vídeo por RTMP. Os espectadores autorizados assistem por WebRTC ou HLS e o proprietário pode baixar as gravações em MP4. Cada instalação tem seu próprio banco, acervo e credenciais.

## Instalação com wget

Use uma máquina Ubuntu Server 24.04 x86-64 com IPv4 fixo, internet e espaço em disco. Substitua `IP_DA_MAQUINA` pelo IPv4 que os espectadores vão acessar. Execute em um terminal da máquina de destino.

```bash
wget -O setup.sh https://raw.githubusercontent.com/blackskull1586/sma-central/main/setup.sh
sudo bash setup.sh IP_DA_MAQUINA
```

O script obtém uma revisão específica do código, verifica os arquivos e instala em `/opt/SMA_Central`. As imagens Docker e as dependências do sistema são baixadas durante a instalação. O instalador confere o certificado e a página de login antes de exibir o endereço HTTPS.

| Porta de destino | Uso |
| --- | --- |
| TCP 80 e 443 | Emissão e renovação do certificado e acesso à Central |
| TCP 19350 | Recepção RTMP dos transmissores |
| UDP e TCP 28080 | Mídia WebRTC dos espectadores |

Direcione essas portas à máquina no firewall ou NAT conforme o serviço utilizado. Para acesso externo atrás de NAT, informe o IPv4 público. O RTMP padrão envia a chave e a mídia sem criptografia de transporte. Para redes restritas, use uma VPN ou limite as origens do envio.

## Primeiro acesso

1. Abra `https://IP_DA_MAQUINA/`. O usuário inicial é **central**. Consulte a senha provisória no terminal privado com `sudo less /opt/SMA_Central/runtime/bootstrap.json`. Troque-a no primeiro login.
2. Abra **Servidores → Novo servidor**, informe nome, identificador e credenciais provisórias do administrador. A criação é imediata. Cada servidor lógico separa contas e canais e compartilha os recursos físicos da máquina.
3. Na lista, escolha **Administrar** para criar ambientes, usuários e canais. Copie a URL RTMP e a chave do canal para o transmissor. Use **Gravações** na Central para consultar e baixar os arquivos após o encerramento da transmissão.
4. Para abrir a Central em janela própria em dispositivos compatíveis, use o comando de instalação do aplicativo no navegador. No Safari do iPhone/iPad, use **Compartilhar → Adicionar à Tela de Início**.

## Usar um domínio opcional

1. No provedor do seu domínio, crie um registro **A** para o IPv4 da VPS, por exemplo `central.seudominio.com.br`. Se publicar também **AAAA**, ele deve apontar para o IPv6 reconhecido pela instalação. Durante a configuração, mantenha os registros apontados diretamente à VPS.
2. Entre na Central pelo IP com a conta proprietária. Abra **Domínios**, informe o nome completo e salve. O painel mostra o resultado da verificação DNS e o estado do HTTPS.
3. Quando o domínio estiver ativo, abra `https://central.seudominio.com.br/` e autentique-se no novo endereço. O mesmo IP continua disponível. A aba permite desativar o domínio ou solicitar nova verificação depois de corrigir o DNS.

**IPv4 privado.** A instalação gera uma autoridade certificadora local e grava o certificado público em `/opt/SMA_Central/SMA_CA_Local.crt`. Confira a impressão SHA-256 indicada e importe essa CA nos dispositivos autorizados. Para um domínio com certificado público, as validações externas precisam alcançar a máquina.

## Verificar e manter

```bash
cd /opt/SMA_Central
sudo docker compose ps -a
sudo python3 deploy/verificar.py
```

Para examinar uma falha, consulte `sudo docker compose logs --tail=60` seguido do serviço indicado no verificador. Em caso de erro na mesma versão, corrija a causa e rode novamente o comando de instalação com o mesmo IP. Preserve o banco, os volumes do Caddy e a pasta `runtime/` em backups protegidos. A política inicial de gravação é 60 GiB de acervo e reserva de 20 GiB de espaço livre. Dimensione disco e link de acordo com as transmissões simultâneas.

O roteiro em [testes/LEIA-ME.md](testes/LEIA-ME.md) verifica criação e isolamento de servidores, transmissão e download com uma fonte sintética. Os créditos e avisos de terceiros estão em [TERCEIROS.md](TERCEIROS.md).
