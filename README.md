# Bot Mãe — versão 2 (Etapa 1: base)

Bot de Telegram que cria e cuida de bots filhos. Cada pessoa cria o próprio bot pelo @BotFather, encaminha o token para o bot mãe e o bot filho já liga, com painel próprio.

O plano completo (acervo, mensagens repetidas, palavras proibidas, parcerias) está em [`docs/PLANO.md`](docs/PLANO.md).

## O que já funciona

**Bot mãe**
- ➕ Criar seu bot: passo a passo do @BotFather; você encaminha a mensagem com o token, o bot apaga essa mensagem, confere o token, guarda criptografado e liga o filho na hora
- 🤖 Meus bots: situação de cada bot (🟢 ligado · 🟡 aviso · 🔴 desligado · ⚠️ token não funciona), ligar/desligar, trocar token, remover
- 🤝 Parcerias: tela explicando como vão funcionar (link próprio: `t.me/SEU_BOT_MAE?start=parcerias`). A última linha do /start leva para ela
- 👑 Admins da plataforma (🔐 Acesso → 👑): você coloca outras contas como admin do bot mãe pelo ID, sem mexer no servidor. Só quem está no `ADMIN_IDS` do `.env` mexe nessa lista
- 🔐 Acesso (você e os admins): fase fechada — só você e quem você liberar pelo ID criam bots. Botão para abrir para todos (até 10 bots por pessoa) e lista de todos os bots
- Avisa o dono quando o token de um filho para de funcionar ou quando outro programa usa o mesmo token

**Bot filho** (o dono manda /start no próprio bot)
- Painel com orientação em cada tela
- 👥 Grupos e canais: botões de adicionar que já abrem com as permissões certas, lista com ✅ / ⚠️ / ❌, verificar permissões, detalhe de cada grupo e "tirar o bot daqui"
- Só o dono e os admins do bot podem colocar o bot em grupos; se outra pessoa tentar, ele sai na hora e avisa
- 👮 Admins do bot: o dono adiciona ou tira pessoas pelo ID
- Telas de Acervo, Mensagens repetidas e Palavras proibidas já explicam o que vem (próximas etapas)
- /id mostra o seu ID e o do grupo

## Instalar no servidor (mesmo droplet do bot de clipes)

1. No PowerShell do PC: `scp "C:\Users\Usuario\Downloads\bot_mae.zip" root@IP_DO_SERVIDOR:~/bot_mae.zip`
2. `ssh root@IP_DO_SERVIDOR`
3. Com `root@ubuntu` na tela:
   ```
   unzip -o bot_mae.zip
   bash bot_mae/servidor/instalar.sh
   ```
   Na primeira vez ele pede o **token do bot mãe** e o **seu ID** e cria o `.env` sozinho.

**Atualizar:** mesmo `scp` e `unzip -o`, depois `bash bot_mae/servidor/atualizar.sh`.

**Ver o que está acontecendo:** `journalctl -u bot-mae -f` (Ctrl+C para sair).

## Segurança

- O `.env` tem o token do bot mãe e a `SECRET_KEY`, que é criada sozinha e protege os tokens dos filhos. **Não apague a SECRET_KEY**: sem ela, todos os bots filhos precisam ser cadastrados de novo. Faça backup do `.env` e da pasta `dados`.
- Nunca mande token no chat com ninguém. Se vazar, `/revoke` no @BotFather e use 🔄 Trocar token.

## Testes

```
pip install -r requirements.txt
python tests/test_bot.py
```
Roda tudo com um Telegram falso. Precisa terminar com "62/62 passaram" ou mais.

## Arquivos

```
app/
  __main__.py        liga o bot mãe e os filhos
  config.py          lê o .env (e cria a SECRET_KEY)
  db.py              tabelas
  repo.py            leitura e escrita no banco
  security.py        criptografia dos tokens
  manager.py         liga/desliga os bots filhos (todos no mesmo programa)
  middlewares.py     descarta repetidos, identifica o bot filho
  ui.py              botões e telas
  mother/handlers.py telas do bot mãe
  child/handlers.py  painel do bot filho
servidor/            instalar.sh e atualizar.sh
docs/PLANO.md        plano completo
```
