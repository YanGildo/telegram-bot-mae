# Bot Mãe — plano

Bot de Telegram "mãe" que cria e gerencia bots filhos (como o @MultiForwardBot). Projeto novo, separado do bot de clipes (@interactpictures_bot), que continua funcionando até o novo cobrir tudo.

## Ideia principal

- **Bot mãe = fábrica.** A pessoa encaminha a mensagem do @BotFather com o token. O bot mãe confere o token, guarda criptografado e liga o bot filho na hora.
- **Todos os filhos rodam no mesmo programa e servidor** (droplet da DigitalOcean; sobe o plano se crescer).
- **Quem usa:** por enquanto só o Yan e os admins que ele liberar. Depois, abrir para outras pessoas com planos e limites. O banco já nasce preparado (cada filho tem dono e admins).
- **Bot API pura** (sem MTProto/conta de usuário — evita risco de ban).
- **Ponto técnico:** o arquivo de foto/vídeo só funciona no bot que o recebeu. Por isso o **bot filho** precisa ser admin do canal de acervo e guardar as mídias ele mesmo. Assim ele reenvia mesmo que a mensagem original seja apagada.

## Padrão das telas (como o Group Help)

Toda tela do painel traz, junto dos botões, uma orientação curta e direta:
- **No topo:** o que a tela faz, em 1 ou 2 frases.
- **Situação atual:** o que está ligado agora (ex.: "Penalidade: Ban 7 dias · Apagar: Sim ✅").
- **O que cada botão faz:** uma linha por botão quando não for óbvio (ex.: "➕ Adicionar: inclui palavras na lista").
- **Requisitos e limites,** quando houver (ex.: "O bot precisa ser admin com Apagar mensagens").
- Os botões de ligar/desligar mostram o estado (✅ / ❌) no próprio botão.
- Ao pedir algo digitado, o bot mostra um exemplo do formato e lembra do /cancelar.
- Botão **❓ Ajuda** nas telas principais, com a explicação mais completa.

## Funções do bot filho

### 1. Acervo de mídias
- Um canal privado de origem: tudo que for postado lá (foto, vídeo, álbum) entra sozinho no acervo. Nada de cadastro manual.
- O bot guarda só a referência do arquivo (leve: milhares de mídias não pesam).
- **Ordem:** sequencial ou aleatória (sem repetir até usar todas; histórico das últimas).
- **Álbuns** continuam agrupados quando repostados.
- **Modelo universal do acervo** (igual às mensagens repetidas, mas do acervo): texto, mídia extra e botões de URL aplicados a todo post que sair do acervo. Legenda: manter a original, fixa ou sorteada de uma lista.
- **Limite de mídias:** definido pela pessoa (sugestão padrão: 100).
  - **Rotativo:** ao passar do limite, a mídia nova substitui a mais antiga (ex.: limite 50, a nº 51 entra e a nº 1 sai).
  - **Ou travado:** ao chegar no limite, para de aceitar novas.
- **Zerar acervo:** tira todas as mídias atuais da fila (não apaga nada do canal). A partir daí só contam as mídias postadas depois de zerar. As antigas ficam arquivadas, com opção de restaurar.
- **Remover uma mídia** da fila sem apagar do canal.
- Painel: total, fotos, vídeos, já publicadas, não usadas, última e próxima postagem, destinos ativos.
- Comandos: /status, /pausar, /retomar, /proxima, /modo_aleatorio, /modo_sequencial, /intervalo, /destinos.
- Idempotência: reiniciar o bot não perde a posição da fila nem publica duas vezes.

### 2. Agendador (motor único para acervo e mensagens repetidas)
Cada programação escolhe a fonte: **acervo** ou **mensagem fixa**. E define:
- frequência: a cada X minutos/horas ou a cada N mensagens no grupo;
- cronograma (horário de início) e janela de horários permitidos;
- dias da semana / dias do mês; data de início e data final;
- máximo de posts por dia;
- fixar a mensagem (com ou sem notificar); apagar a anterior; exclusão cronometrada;
- tópico do grupo;
- destinos (grupos/canais), cada um liga/desliga; modo por destino.

### 3. Mensagens repetidas (como o Group Help, sem limite)
- Quantas quiser por grupo, cada uma com texto, mídias, botões de URL, "mídia abaixo do texto", visualização completa.
- Lista com situação (✅/❌), cronograma, repetição e começo da mensagem; botões ativar/desativar/excluir.

### 4. Palavras proibidas (como o Group Help, sem limite)
- Listas sem limite.
- Punições: desativado, advertência (X advertências antes de punir), kick, silenciar, ban, ban com duração; apagar a mensagem sim/não.
- Busca: palavra exata ou contida; ignora acentos e maiúsculas.
- Configuração por grupo; registros no canal de logs.

### 5. Parcerias (troca de divulgação entre bots filhos)
Duas pessoas, cada uma com seu bot filho, combinam postar conteúdo uma no grupo da outra.

- **Convite por código, nunca por token.** Na aba 🤝 Parcerias, A toca em "Convidar" e recebe um link/código. B abre no bot dele e vê a proposta. Token não é compartilhado com ninguém (quem tem o token controla o bot).
- **Proposta = acordo:** frequência mínima entre posts (ex.: 30 min), máximo de posts por dia, janela de horários, o que pode ir (texto, mídia, botões), em quais grupos de cada lado, data de início/fim. B aceita, recusa ou faz contraproposta. Os dois veem o acordo final.
- **Quem posta:** cada lado coloca o bot do parceiro no(s) grupo(s) combinados (o painel gera o link de adicionar e confere se entrou). O bot de A posta direto no grupo de B. Motivo: o arquivo de mídia só funciona no bot que o recebeu, então o bot de B não conseguiria repostar a mídia de A.
- **O sistema garante o acordo:** como todos os filhos rodam na mesma plataforma, o agendador do parceiro não consegue postar acima do combinado (frequência, limite diário, horários, tipo de conteúdo). Se algo passar, o bot do dono do grupo apaga.
- **Conteúdo do parceiro:** usa o mesmo agendador (mensagem fixa ou acervo dele), limitado pelo acordo.
- **Aba Parcerias mostra:** lista de parceiros, situação (ativa ✅ / pausada ⏸ / encerrada ❌), grupos ativos de cada lado (bot do parceiro ainda está lá e com permissão), posts de hoje / do acordo, último post, avisos de problema.
- **Pausar ou encerrar** a qualquer momento por qualquer lado; ao encerrar, o bot do parceiro pode ser removido do grupo automaticamente.
- Depende de outras pessoas terem bot filho na plataforma (ou do Yan criar os bots para os parceiros).
- **Marca d'água obrigatória nas parcerias (grátis ou premium):** todo post feito por parceria leva, na última linha, um rodapé curto com link para o bot mãe. Ex.: "🤝 Parcerias automáticas no Telegram · @NomeDoBotMae" (frase final a definir quando o nome do bot mãe estiver escolhido). Não dá para remover nem editar.
  - O link abre o bot mãe direto na **tela de Parcerias** (link próprio, `t.me/BotMae?start=parcerias`), explicando como funciona e com o botão para criar o bot e começar.
  - O link carrega quem trouxe a pessoa (`start=parc_<id do bot>`), para saber de onde vêm os novos usuários (e, no futuro, dar bônus por indicação).
  - Em álbum, vai na legenda; quando a legenda já estiver no limite do Telegram (1024 caracteres), o texto do post é encurtado para a marca caber.
  - Nas outras funções (acervo, mensagens repetidas), a marca pode existir no plano grátis e sumir no premium — decidir quando montar os planos.

### 6. Funções do bot atual (copiar o código já testado)
Anti-spam por @ (com espera), ban em todos os grupos (só ban feito pelo dono/admin), canal de registros, envio com moderação, publicar/agendar, republicar, grade diária.

## Etapas

| Etapa | Entrega |
|---|---|
| ✅ 1. Base | Repositório novo; bot mãe criando e ligando filhos; painel do filho; registro dos grupos/canais |
| ✅ 2. Acervo | Canal de origem, fila sequencial/aleatória, limite e rotação, zerar, modelo universal, painel e comandos |
| ✅ 3. Mensagens repetidas | Agendador completo com mensagem fixa |
| ✅ 4. Palavras proibidas | Listas, punições, por grupo |
| 5. Funções do bot atual | Anti-spam, ban em todos, logs, moderação, grade |
| 6. No ar | Mesmo servidor, serviço separado; testes; passo a passo de atualização |
| Depois | Abrir para outras pessoas: planos, limites, pagamento |
| ✅ Parcerias | 🤝 Parcerias entre bots filhos, com acordo, convite e marca d'água (versão 7) |

## Situação (versão 3)

Etapas 1 a 4, parcerias, canal de registros, entrada por nome/bio, ban em todos e anti-spam prontos (versão 7). Pendente: abrir para outras pessoas com planos e pagamento (decidir preço, limites e forma de pagamento). O envio de clipes com moderação continua no bot de clipes, separado.

## Pendente que existia antes da Etapa 1 (resolvido)

1. Repositório vazio e privado no GitHub (ex.: `telegram-bot-mae`).
2. Criar o bot mãe no @BotFather (token vai direto no `.env` do servidor, nunca no chat).
3. Idioma do painel: só português ou também inglês/espanhol.

## Referências
- MultiForward: https://multiforward.me/ (encaminhamento de um canal de origem para vários destinos, filtros, agendamento, criação de bots próprios).
- Group Help: palavras proibidas e mensagens repetidas (prints de referência enviados pelo Yan em 18/11/2026).
