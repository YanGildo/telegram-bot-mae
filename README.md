# Bot Mãe — versão 6

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
- Cada tela explica o que cada botão faz
- Comandos no privado do filho: /status · /pausar · /retomar · /proxima · /id · /ajuda · /cancelar

**🗂 Acervo**
- Canal de origem: tudo que você posta lá (foto, vídeo ou álbum) entra sozinho no acervo; editar a legenda no canal atualiza no acervo
- Limite (25, 50, 100, 200, 500 ou sem limite): **rotativo** (a nova entra e a mais antiga sai) ou **travado** (avisa que lotou)
- Ordem **aleatória** (nunca repete a última e só repete depois de usar todas) ou **sequencial** (recomeça ou para no fim)
- Legenda: original, fixa, sorteada de uma lista ou nenhuma + texto universal no fim + botões de link + mídia abaixo do texto; 👀 prévia
- 🗂 Ver mídias (tira da fila sem apagar do canal) · 🧹 Zerar (só contam as novas) · ♻️ Restaurar · ⏸ Pausar
- ⏱ Programações do acervo: quantas quiser, cada uma com destinos e ritmo próprios
- 📚 Álbuns **inteiros** (o álbum sai junto) ou **soltos** (cada foto/vídeo vira um post, com a legenda do álbum)
- Qualidade original: o bot reenvia o mesmo arquivo do Telegram, sem recomprimir. Para escapar da compressão do próprio Telegram, poste no canal como **arquivo**
- A mesma mídia postada duas vezes só entra uma vez
- Sem envio duplicado: cada horário só é publicado uma vez, mesmo com dois cliques em Enviar agora; nos destinos, 🔁 avisa quando outra programação ativa publica no mesmo lugar
- Aviso quando o acervo está retomado mas não tem programação ativa

**🔁 Mensagens repetidas** (sem limite, telas como as do Group Help)
- ✍️ Personalizar: texto (com formatação e emoji premium), foto/vídeo/álbum, botões de link, mídia abaixo do texto, 👀 visualização
- Cronograma, repetição (a cada X minutos/horas ou a cada N mensagens no grupo), dias da semana, horário permitido, período (data início/fim), máximo por dia
- Fixar (com ou sem notificar), apagar a anterior, exclusão automática, destinos, 🚀 enviar agora
- O mesmo agendador roda as programações do acervo

**📋 Canal de registros** (painel → 📋 Registros): canal ou grupo onde o bot conta o que fez
- 🔇 silenciados (botões 🔊 Desmutar e 🚷 Banir), 🚷 banidos (♻️ Desbanir), ❗ expulsos, ⚠️ advertências (🧹 Zerar)
- 🚪 pedidos de entrada aprovados/recusados e o motivo
- 🔁 aviso quando o acervo ou os conteúdos de uma programação completam a rodada (sem canal, o aviso vai no seu privado)

**🚪 Entrada por nome e bio** (painel → 🚪 Entrada, por grupo/canal): com link de convite "pedir aprovação", o bot aprova ou recusa olhando nome e bio
- Palavras bloqueadas (recusa ou deixa esperando você), palavras de liberação (só aprova quem tem uma), exigir @

**Programações**: ✏️ nome para cada uma; 📚 vários conteúdos numa mensagem repetida (cada envio usa um, em ordem ou sorteado), enviados ou encaminhados direto no bot, separados do acervo

**🔨 Ban em todos os grupos** (veio do bot de clipes)
- Você (ou um admin do bot, inclusive anônimo) bane alguém pelo Telegram num grupo → o bot bane pelo ID em todos os outros grupos/canais onde é admin (menos o canal de registros). Bans do Group Help e de outros admins não espalham; admins do bot nunca são banidos
- `/banirtodos ID` no privado, ou respondendo à pessoa no grupo (o comando some); `/desbanirtodos ID`
- Registro com ♻️ Desbanir de todos; grupo novo recebe os bans existentes; 📥 importar IDs (ex.: do /exportar do bot de clipes)

**🧹 Anti-spam** (veio do bot de clipes): apaga mensagens de @ da lista (quem postou, repasses, texto/botões divulgando), na hora ou depois de 1, 5 ou 10 min

**🚫 Palavras proibidas** (por grupo, sem limite de palavras)
- Punição: desativado, advertência (1 a 5, depois silencia/kick/ban), kick, silenciar, ban; duração (1 h a 30 dias ou para sempre)
- Apagar a mensagem ou não; busca "contém" ou "palavra exata"; ignora acentos e maiúsculas; frases também
- Admins do grupo e do bot nunca são punidos; o aviso no grupo some sozinho em 1 minuto
- /id mostra o seu ID e o do grupo
- 🧪 Testar emoji premium (em 👥 Grupos e canais): mostra onde o emoji premium aparece e onde vira emoji comum

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
Roda tudo com um Telegram falso. Precisa terminar com "160/160 passaram" ou mais.

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
  child/acervo.py    acervo (canal de origem, limite, legenda)
  child/schedules.py telas das programações e mensagens repetidas
  child/scheduling.py agendador (próximo envio, publicar, exclusão automática)
  child/words.py     palavras proibidas (e botões do canal de registros)
  child/joins.py     entrada por nome e bio, tela do canal de registros
  child/logs.py      envio para o canal de registros
  child/moderation.py ban em todos os grupos e anti-spam
  child/content.py   montar e enviar posts (mídias e botões)
servidor/            instalar.sh e atualizar.sh
docs/PLANO.md        plano completo
```
