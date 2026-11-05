# Changelog

Tudo o que mudou na LineOps, da versão mais nova para a mais antiga.

## 3.0.4
* **Repositório público:** a webhook padrão do Discord saiu do código. Ela entra só no instalador, gravada pelo
  workflow de Release a partir do segredo `LINEOPS_DISCORD_WEBHOOK`. Rodando pelo código, cadastre a sua em Ajustes ›
  Discord. O README ficou curto; o histórico de versões mora neste arquivo.

* **"Aeronave está em…" no Mais voos.** Informe o aeroporto (ICAO ou IATA) e a chave do primeiro dia escolhido começa
  ali, em vez de onde a escala anterior deixou. Vale só para esse dia: os seguintes seguem a escala nova. Jornada e
  repouso dos dias anteriores continuam contando, e o diagnóstico registra a troca ("começa em SBRF, onde você disse que
  a aeronave está; a escala anterior terminava em SBGR").

## 3.0.3

* **Mais voos a partir de um dia.** Na Escala, ao lado de "Texto", o botão "Mais voos": escolha um ou mais dias e o
  mínimo de pernas em cada um (1 a 6). A escala é regerada a partir do primeiro dia escolhido, com os mesmos parâmetros
  da escala atual (folgas pedidas, pernoites pedidos, continuações). O que vem antes do dia não muda, e a parte nova
  continua de onde o avião estava. Folga regulamentar no dia escolhido troca de dia; reserva não.
* **Mínimo de pernas no gerador** (Gerar escala › Seu pedido): o dia vira dia de voo e o motor só aceita sequências com
  pelo menos aquela quantidade de pernas reais do RPL, dentro das regras. O diagnóstico diz, dia a dia, se encaixou;
  quando não cabe, a escala sai mesmo assim e diz por quê.
* **Correção:** a conferência do pernoite pedido usava a hora UTC em vez da local. Em voo que chega depois das 21h
  (horário de Brasília), o pedido podia ser dado como atendido ou não atendido errado, e a "melhor de 4 tentativas"
  escolhia mal. Corrigido.

## 3.0.2 beta 9

* **Dia da escala: as ações ficam dentro de cada voo.** Reservar, Já fiz, Abrir o voo, Pedir troca e a caixa PERNOITE
  (Discord) aparecem no cartão da própria perna, só as que fazem sentido naquele momento. Acabou a fileira de seis
  botões e três caixas no fim do dia.
* **Frota com resumo e filtros.** Em cima, quantas aeronaves estão disponíveis, com restrição e indisponíveis (clique
  para filtrar); busca por matrícula, configuração ou posição; filtro por tipo. As que precisam de atenção vêm
  primeiro, com o motivo embaixo da situação.

## 3.0.2 beta 8

* **Escalas reais de pilotos como referência.** Quatro escalas da LATAM e uma da Azul (setembro a novembro de 2026,
  sem nome de ninguém) ficam em `lineops/aeroscale/data/escalas_reais.json` e são usadas em dois lugares:
  * **Continuações do avião:** uma continuação que aparece numa escala real (ex.: TAM3376 GRU→REC seguido do TAM3371
    REC→PNZ numa quarta; TAM3676 → TAM3371 numa sexta) entra com confiança alta, por cima da estimativa do RPL e do
    ADS-B, e aparece na coluna "Escala real" com os dias em que foi vista. As escalas que você importar em Gerar escala
    › Importar escala real contam do mesmo jeito.
  * **Perfil de realismo:** pernas por dia da LATAM recalibradas com 57 dias de voo (1 perna 33%, 2 30%, 3 31%, 4 5%);
    Azul com 67 dias.
* **Importar escala real** aceita números separados por espaço (`AEP-GIG-GRU-FOR (8045 3699 3252)`) e blocos separados
  por barra (`REC-CNF(4605)/CNF-PMW-CNF (4408,4409) +1`). Testado com as cinco escalas acima: todas entram inteiras.

## 3.0.2 beta 7

* **Os avisos de pernoite no Discord seguem a escala.** Ajustes › Discord › "Pernoites automáticos": a cada atualização
  da escala (gerar, troca aprovada, corte, INATIVO tirado), todo dia que termina em pernoite fora da base é enviado
  sozinho. Os avisos já enviados são editados quando o voo muda (horário, próxima apresentação, hotel) e apagados quando o
  voo sai da escala. Mesmo com o automático desligado, os já enviados são mantidos em dia. "Sincronizar com a escala
  agora" força a conferência. Voo marcado à mão que a escala não vê como pernoite aparece como "marcado pelo piloto",
  sem o tempo no solo (que confundia: "0 h 40 no solo").
* **Gerar escala mais simples:** só o essencial à vista (período, tripulante, seu pedido). Aeroportos, pernas e tempos,
  regras de tripulação e reserva ficam em "Opções avançadas", recolhidas (a LineOps lembra se você abriu). O botão
  "Gerar escala" acompanha a rolagem.

## 3.0.2 beta 6

* **Biblioteca técnica** (tela nova, ou Ctrl + K › "Biblioteca"). Importe os seus PDF: MEL, CDL, QRH, FCOM. O texto
  fica página a página no banco da companhia atual (dados/carreiras/XXX.db), com busca (SQLite FTS5). Os documentos de
  uma companhia não aparecem nas outras. **Nada vem com a LineOps**: as MEL/CDL/QRH são documentos da companhia,
  protegidos; o arquivo não é copiado nem publicado, só o texto fica no seu banco.
  * PDF digitalizado (imagem, sem texto) importa, mas a busca não acha nada nele. A LineOps avisa.
* **MEL item a item.** A MEL no formato AMOS (como a da GOL, "Minimum Equipment List") é lida item a item: código ATA
  (21-10-02-03), título, categoria de reparo (A/B/C/D), instalados, exigidos para despacho, condições (remarks or
  exceptions), placard e os procedimentos (M) e (O). Na aba MEL dá para navegar por capítulo ATA ou buscar pelo nome ou
  pelo código. Cada item mostra a página do PDF: confira lá antes de usar, é transcrição automática.
* **Discrepância pela MEL.** Na Frota, ao registrar uma discrepância, escolha o item da MEL importada: o código, o título,
  a categoria e o prazo de reparo (definição 22 da MEL: B 3 dias, C 10, D 120; A, o que a observação disser) vêm dele.
  Item com procedimento operacional (O) vira restrição "apenas informa" com o texto do procedimento. A disponibilidade da
  aeronave (Frota e resumo do dia) já considera o prazo vencido.
* Dependência nova: `pypdf` (pip install -r requirements.txt). O instalador do Windows já leva.

## 3.0.2 beta 5

* **Tirar os dias INATIVOS.** O AeroScale coloca dias INATIVOS fora da base (descanso longo, sem voo). Agora dá para
  tirar: em Gerar escala › Dias INATIVOS (todos de uma vez) ou no próprio dia, na Escala. Cada dia é preenchido com
  voos reais do RPL (ida e volta a partir do aeroporto onde você está, dentro das regras de jornada e repouso); se
  nenhum par de voos reais cabe, o dia fica livre no mesmo aeroporto. Nada é inventado e só os dias com INATIVO mudam.
  Para não gerar mais nenhum, desmarque "Dia INATIVO fora da base" no passo 2 do gerador.

## 3.0.2 beta 4

* **Continuações do avião na geração, sozinhas.** Depois de cada voo, o gerador segue para onde o avião costuma ir
  (por exemplo, o TAM3376 GRU→REC segue para Petrolina em alguns dias). Não precisa cadastrar nada.
  * **De onde vem:** o RPL do CGNA (o que sai do mesmo aeroporto de 25 a 240 min depois da chegada) e os rodízios vistos
    no ADS-B (26 dias do adsb.lol). Cada continuação tem confiança alta, média ou baixa. Sem dado, não entra.
  * **Força das continuações** (slider em Gerar escala, padrão 70):
    * 0 desliga tudo.
    * Acima de 0, entram as de confiança alta; a partir de 30, as médias; a partir de 80, as baixas.
    * Cada vez que o voo aparece, a continuação é obrigatória com chance = força × peso da confiança (alta 1, média
      0,85, baixa 0,6). Na força 100, a de confiança alta vem sempre. O sorteio usa a semente da escala: mesma semente,
      mesma escala.
    * O diagnóstico da escala diz quantas vezes a continuação foi seguida. Quando o seu dia acaba no voo (fim da
      jornada, folga, fim do período), o avião segue com outra tripulação: isso não é erro.
  * **Pares invertidos:** ida e volta costumam ter números vizinhos (3376/3375). A LineOps mede o sentido mais comum de
    cada companhia e marca o par que vai ao contrário como "invertido".
  * **Suas continuações** (opcional): Ctrl + K › "Continuações". "Depois do 3376 segue o 3262" ou "segue para qualquer
    voo para PNZ", em todos os dias ou só em alguns. As suas valem sempre, por cima da estimativa.
* **Pernoite marcado manda no dia:** marcou "ESSE VOO É PERNOITE"? O pernoite do dia passa a ser no destino desse voo,
  na hora: resumo do dia, hotel, carro, despertar e calendário do mês.
* **Bogotá (BOG) e outros aeroportos de fora** agora são reconhecidos pelo código IATA no pernoite e nas trocas.
  Atenção: nem o RPL nem o ADS-B têm voos da GOL, LATAM ou Azul para Bogotá no período, então a escala não consegue
  pernoitar lá (e diz isso, em vez de inventar um voo).

## 3.0.1

* **Cabine noturna (visual novo).**
  * Base azul-noite de painel de voo, com uma grade de carta quase invisível atrás das telas.
  * As cores de aviônica têm o significado da cabine: ciano é dado vivo, verde é ok, âmbar é atenção e vermelho é alerta.
    A cor da companhia continua nos botões e na navegação.
  * **Faixa de instrumentos** no topo de toda tela: hora UTC e local correndo, a próxima apresentação em contagem
    regressiva (âmbar a menos de 2 h), o próximo voo e o voo em andamento.
* **Estatísticas** (tela nova): aeroportos mais usados (total, partidas e chegadas), rotas, onde você mais pernoita,
  partidas por hora local, etapa mais longa, dia mais cheio e o voo que mais se repete. Vale para a escala (o
  programado) ou o diário (o voado), no mês ou no período todo.
* **O que era só do cmdt079 agora é de todos:** a caixa "ESSE VOO É PERNOITE" e o envio para o Discord (Ajustes ›
  Discord). A foto fixa do cmdt079 continua só como a foto padrão dele.
* **Escalas geradas nas estatísticas do projeto:** cada escala gerada manda um resumo anônimo para a webhook do
  projeto. Vai companhia, tipo, base, período, pernas, horas, aeroportos e rotas mais usados, a versão e um código
  aleatório da instalação. Não vai nome de usuário, e-mail nem IP. Para desligar: Ajustes › Estatísticas do projeto.
* **Muito mais tripulação:** 318 nomes de copiloto e 724 de comissário, todos fictícios.

## 3.0

**Mais rápida e mais certa**
* **Uma leitura só da escala** (`lineops/escala.py`). O painel de limites, as trocas, o mês, a exportação e o corte da
  escala leem as mesmas pernas, com a mesma origem do dado. Na 2.x, cada tela tinha a sua consulta e cada uma esquecia
  uma coisa diferente: daí vieram vários bugs (tripulação composta ignorada, pernas de sobreaviso sobrando depois do
  corte).
* **Performance:** índices novos no banco (voo da perna, pernas vivas, PIREP do voo, dias especiais, reservas), cache de
  páginas do SQLite em memória, tela compactada com gzip (o `app.js` cai de ~280 KB para ~80 KB) e script/estilo com
  `?v=` guardados no cache do navegador até a próxima versão.

**Novidades de uso**
* **Paleta de comandos:** Ctrl + K, ou "Buscar" na barra lateral. Dá para ir a qualquer tela, rodar uma ação, abrir um
  dia ("12/11") ou achar uma matrícula. Atalhos na Escala: ← → (dia, semana ou mês), T (hoje), D/S/M (modo). G abre
  "Gerar escala" e ? mostra os atalhos.
* **Escala na agenda:** Escala → Texto → **Agenda (.ics)**. Funciona no Google Agenda, no Outlook e no calendário do
  celular. Cada perna entra no horário certo, e folga, reserva e sobreaviso entram como dias inteiros. Importar de novo
  atualiza em vez de duplicar.
* **Conquistas** (Carreira), tiradas só do diário de bordo: voos, horas, voo noturno, pouso manteiga (critério do
  fabricante), sequência sem pouso duro, aeroportos, as cinco regiões do Brasil (IBGE), exterior, matrículas, voos
  online e tipos.
* **Perfil** (Ajustes): qualquer usuário pode pôr foto (JPEG, PNG ou WebP, até 2 MB, guardada na pasta de dados) e
  trocar o nome de exibição.
* **"Novidades da versão"** aparece uma vez depois de atualizar.
* Também: as folgas do seu jeito no gerador e o "Esqueci a senha" (2.5.3 e 2.5.4).

**Correções**
* Passaporte: aeroporto estrangeiro (ex.: Ezeiza) contava como Brasil, porque o catálogo do AeroScale guarda o país no
  campo da UF. Agora conta o país certo.

## 2.5.4

* "Esqueci a senha": se o usuário digitado não existe neste computador, a tela diz quais existem (só depois do código
  certo) e o código continua valendo. Cada LineOps tem o próprio usuário: o de outro PC não existe aqui.

## 2.5.3: esqueci a senha e folgas do seu jeito

* **Folgas** (Gerar escala → 1. Período): **Automáticas** (as regras do AeroScale, como sempre), **Sem folgas** ou **Só as
  que eu escolher** (calendário do período: toque nos dias). Nos dois últimos modos, o interruptor "Folgas
  regulamentares" do próprio AeroScale fica desligado: o motor não sorteia folga nem bloco longo, a "Folga" fixa do dia da
  semana sai, e o painel não cobra folga mínima nem dias seguidos (Lei 13.475, arts. 50 e 51). Jornada, repouso e horas do
  mês continuam valendo, então "Sem folgas" ainda pode ter dia sem voo quando esses limites apertam (o diagnóstico diz
  qual). Para voar todo dia, desligue também "Limites de 80 h…" nas regras de tripulação.

* A LineOps não tem senha padrão: cada instalação cria o usuário no primeiro acesso e guarda só o hash (scrypt). A senha
  não se recupera, se troca, e nada da carreira é apagado.
* **No app:** na tela de entrada, **Esqueci a senha** → **Gerar código**. O código de 6 dígitos vai para o arquivo
  `REDEFINIR-SENHA.txt` da pasta de dados (no Windows ele abre sozinho). Digite o código e a senha nova. Vale 10 minutos
  e 5 tentativas, e só funciona no próprio computador (pedido vindo da rede é recusado).
* **Pela linha de comando:** `redefinir-senha.bat` (ou `python -m lineops redefinir-senha [--usuario NOME]`).
* Trocar a senha encerra as sessões abertas desse usuário. O token do ACARS continua valendo.
* Correções da 2.5.2 achadas pelo CI: o cliente ACARS abre sessão de novo (antes dava erro 500 na carreira), e o painel
  mostra o aviso de independência da companhia escolhida.

## 2.5.2

* **Código do piloto por companhia.** Embaixo do nome aparecia sempre GOL001, em qualquer modo. O número fica e o prefixo
  segue a companhia da sessão: GOL001 na GOL, TAM001 na LATAM, AZU001 na Azul (também no painel e no rodapé do Discord).
* **Foto de perfil do cmdt079**, fixa no app a pedido dele (`lineops/assets/avatars/`). Ela só sai pela API para o
  próprio usuário (`/api/me/avatar`); os outros continuam sem foto.

## 2.5: pernoite pedido, Discord do cmdt079, frota de referência e carga da GOL

* **Pernoite pedido** (Gerar escala → 7. Seu pedido). Escolha a noite e o aeroporto (ICAO ou IATA, até 8). O motor usa o
  próprio recurso do AeroScale de terminar o dia num aeroporto obrigatório (`plan_day(must_end=…)`): só voos reais do RPL,
  com jornada, repouso e conexão conferidos. Se o motor tinha posto folga regulamentar ou sobreaviso naquele dia (ou no
  seguinte), ela troca com um dia de trabalho próximo, sem passar do máximo de dias seguidos. Folga pedida, folga fixa do
  dia da semana, reserva e indisponibilidade não mudam. São até 4 tentativas com sementes derivadas da sua (a mesma
  semente dá a mesma escala), e fica a que atende mais pedidos sem erro de jornada. O diagnóstico diz, noite por noite, se
  deu certo e por quê, e qual foi a semente. Sem caminho, o dia segue normal e o pedido aparece como não atendido.
  Nada é inventado para cumprir o pedido.
* **“ESSE VOO É PERNOITE” no Discord** (só para o usuário `cmdt079`). Já vem com a webhook padrão (2.5.1). Em
  Ajustes › Discord dá para trocar por outra e voltar para a padrão.
  No dia da escala, cada voo ganha a caixa; o voo que termina em pernoite pela escala aparece destacado. Marcada, ela manda
  um embed com voo, trecho, data, chegada (hora local e Z), cidade do pernoite e tempo no solo, próxima apresentação e
  hotel (o de Ajustes › Pernoites; sem cadastro, a mensagem diz que falta). Marcar de novo edita a mesma mensagem e
  desmarcar apaga. Não menciona ninguém (`allowed_mentions` vazio). Outros usuários não veem a caixa e a API responde 403.
* **Frota de referência** (as listas que você mandou): LATAM A319 (18), A320 (57) e A320neo (38), e GOL B738 (74). Ficam
  standby (ISIS/ISFD/analógico), DDRMI, DCDU, ROW/ROPS, DRAMS, FMC (Collins/Honeywell), empuxo e SFP, cada valor com a
  fonte “lista informada pelo usuário (nov/2026)”. PS-GF* (cargueiros) ficaram de fora. A frota entra uma vez: matrícula
  que você já tinha não é recriada e só ganha os campos vazios, e o que você editou nunca é sobrescrito. A configuração
  aparece na lista da frota, na ficha da aeronave e na escolha da aeronave ao reservar.
* **Voos de carga da GOL (GLO9xxx)**: por padrão ficam fora da escala e das opções de troca. Para voar carga, marque
  “Incluir voos de carga” no gerador (só no modo GOL).
* **Consertos:** o painel de limites e a checagem das trocas não liam a origem da perna. Por isso, etapa internacional
  longa observada no ADS-B (ex.: EZE→FOR, 5 h 30) não levava em conta a tripulação composta, como o AeroScale faz, e
  acusava jornada acima do limite. Além disso, apagar a escala daqui pra frente (ou toda ela) deixava para trás as pernas
  do sobreaviso acionado.

## 2.4.2: escala em texto

* **Exportar a escala em texto** (Escala → botão **Texto**), como circula entre tripulantes, um dia por linha:
  `(01) GRU-REC-GRU (1586,1587)`, `(05) FOLGA`, `(13) RESERVA GRU`, `(11) SOBREAVISO · ACIONADO: …`, com o bloco das
  reservas no fim. É o exportador do AeroScale (`scheduler.export_txt`), com os dois estilos dele (“como a companhia
  escreve” e “clássico”) e a opção de horários, mas lendo a escala que está na sua carreira agora: trocas, cortes e meses
  refeitos aparecem. Escolha o mês aberto, a escala inteira ou outro período; copie ou baixe o .txt. Dia que nenhuma
  escala cobre (depois de apagar ou cortar) sai como `SEM ESCALA`, nunca como folga. Teste: o texto da carreira é igual,
  caractere por caractere, ao do AeroScale para a mesma escala.
* **Consertado:** reimportar a mesma escala do motor depois de um corte trazia de volta as reservas e as folgas cortadas, e
  regerar o mesmo mês deixava o fim da escala preso no dia do corte. Agora a escala nova vale inteira e a reimportada
  respeita o corte.

## 2.4.1: gerador completo do AeroScale e "apagar todas as escalas"

* **Geração de escala consertada.** O defeito: o ajuste “não reutilizar voos já atribuídos em outras escalas” do AeroScale
  contava as **suas próprias** escalas anteriores do mesmo período como ocupadas. Cada vez que você gerava de novo, sobravam
  menos voos e a escala saía mais vazia (às vezes zerada). Agora a escala nova substitui a antiga do mesmo período dentro do
  motor; o ajuste continua valendo para escalas de outros períodos. Teste: três gerações seguidas do mesmo mês saem cheias.
* **Seletor de dia da escala consertado.** As setas e o “Hoje” mudavam a data, mas a data que estava no endereço
  (`#/escala/AAAA-MM-DD`) voltava por cima. Agora o endereço acompanha o dia escolhido.
* **Todos os ajustes do gerador do AeroScale** na tela Gerar escala, com os mesmos nomes, limites e padrões do `generator.js`
  (modo tripulante):
  1. Período (mais “A partir do dia”, “Substituir” e “Começar de onde o avião está”, que já eram da LineOps).
  2. Tripulante: habilitação, conexão mínima com troca de aeronave, voltar como extra, realismo das escalas reais
     (sobreaviso por mês e chance de acionar, bloco longo de folga, dia INATIVO).
  3. Aeroportos: base, retorno à base (livre / pernoitar na base / terminar na base), preferência com força e aeroportos a evitar.
  4. Pernas e tempos: duração mínima e máxima da perna, pernas por dia, solo mínimo (automático ou fixo) e máximo,
     descanso noturno, janela de partidas, **máximo de pernas por dia da semana** (Padrão / Folga / até 1…8), chance de
     chaves de 2 e 3 etapas, compatibilidade de tipo, projeção do RPL, não reutilizar voos, internacionais (com a % opcional),
     modelo de rodízio, pernas de posicionamento e chance de anormalidade.
  5. Regras de tripulação: RBAC 117 / Lei 13.475 estrita / desligado, folgas mínimas, dias seguidos, folgas regulamentares,
     folga na base, madrugadas e limites mensais. Botão **“Salvar dias da semana e regras como padrão”** (vale para as
     próximas escalas, como no AeroScale).
  6. Reserva automática: blocos, duração, chance de ser na base, de acionamento e duração do acionamento.
  7. Seu pedido: folgas pedidas, período do dia, evitar madrugada e semente.
  A tela lembra o que você usou da última vez em cada companhia.
* **Apagar todas as escalas** (Gerar escala → painel da direita). Mostra quantas etapas, reservas e blocos saem, pede para
  digitar APAGAR e faz um backup completo antes. Saem pernas, folgas, reservas, sobreavisos e pedidos de troca pendentes;
  reservas de voo ainda não voadas são canceladas. O diário de bordo, os voos feitos, os salários e as mensagens ficam.
  Bloqueado se houver voo em andamento. Reimportar a mesma escala do AeroScale não traz as pernas de volta.

## 2.4: modo Azul, o mês em calendário e o mapa

**Modo Azul** (ativo): carreira própria, com o RPL da Azul embutido. No RPL ela voa E195-E2, A320neo, ATR 72-600, E195,
A321neo e A321. O perfil de escala é o calibrado no AeroScale com 3 escalas reais de comandante Azul (base REC): apresentação
de 50 min, ônibus Viracopos ↔ São Paulo. Hubs sugeridos: Viracopos, Confins e Recife.
* **Comissários** pela configuração da Azul (seatmaps.com, conferido com a Wikipédia): ATR 72-600 70 → 2,
  E195 118 → 3, E195-E2 136 → 3, A320neo 174 → 4, A321neo 214 → 5, A330-200 271 → 6, A330-900neo 298 → 6.
* **Pouso:** ATR e E-Jets usam só o limite do trem, 600 fpm. Para o ATR, é o limite de energia do trem citado no
  ATSB AO-2017-111. Os números de G dos manuais (AMM) desses tipos não estão em nenhuma fonte pública; a G aparece, mas não
  decide sozinha.
* **Limite de horas:** turbo-hélice, 85 h de voo no mês; jato, 80 h (Lei 13.475, art. 33, pelas regras do AeroScale).

**Escala → Mês:**
* um calendário do mês: tipo de cada dia, trecho, etapas, bloco e onde você dorme;
* um **mapa** dos trechos, com as coordenadas do catálogo do AeroScale e a base em destaque;
* os **limites do mês** em barras: horas de voo, horas de jornada e folgas contra o mínimo da lei; madrugadas e dias seguidos.

**Painel:**
* contagem regressiva para a próxima apresentação;
* os limites do mês.

**Carreira:**
* **Passaporte:** cada aeroporto onde você pousou, com a primeira vez e quantas vezes;
* o mapa de tudo o que você já voou;
* horas por tipo de avião.

**Diário:** botão **Exportar CSV (Excel)**.

**Fechamento do mês:** no dia 1, a Chefia manda o balanço do mês que acabou:
* voos e horas;
* aeroportos novos;
* média e melhor toque, e pousos duros;
* pontualidade e horas por tipo.

## 2.3: modo LATAM

Na tela das companhias, **Entrar no modo LATAM** abre uma carreira separada da GOL, com escala, frota, diário, salário,
equipe e mensagens próprios. O login, o celular e os Voos Externos continuam os mesmos.

* **Escala:** o mesmo motor do AeroScale, com o RPL da LATAM (TAM) embutido e atualizado como o da GOL, e o perfil
  calibrado com escala real de comandante LATAM (base GRU). No RPL do CGNA, a LATAM voa A319, A320 e A321 no Brasil; os
  internacionais (787-9, 777-300ER) entram quando o ADS-B os observa, igual no AeroScale. Bases sugeridas: GRU, BSB e CGH,
  as que mais têm partidas no RPL.
* **Pouso:** os critérios do fabricante do avião que você voou.
  * A320 (A319, A320, A321, neo): o relatório LOAD<15> da Airbus pede inspeção acima de 2,6 G ou 9 ft/s (540 fpm) no
    toque; acima do peso máximo de pouso, 1,7 G ou 6 ft/s.
  * 737: 2,2 G ou 600 fpm (AMM 05-51), como antes.
  * 777 e 787: o limite de projeto do trem, 600 fpm. As fontes da LineOps não trazem o número de G do manual desses tipos.
* **Comissários:** pelo RBAC 121.391, com os assentos da configuração da LATAM Brasil (seatmaps.com): A319 144 → 3,
  A320/A320neo 180 → 4, A321/A321neo 224 → 5, 787-9 300 → 6, 777-300ER 410 → 9.
* **Equipe:** outras pessoas (também fictícias), com os mesmos setores. As cores são as da companhia; o logotipo é um
  arquivo que você mesmo coloca.
* **Salário:** a mesma CCT da aviação regular. Se o seu contracheque for diferente, os valores são editáveis.

## 2.2: mudar de base a partir de onde o avião está

Em **Gerar escala**, o painel **Mudar de base / recomeçar daqui** mostra onde o seu avião fica: o fim do último voo da
escala de hoje (sem voo hoje, o último antes de agora).

* **Apagar daqui pra frente:** as pernas depois desse voo saem da escala, com histórico, como numa alteração de escala.
  As reservas de voo delas são canceladas, os pedidos de troca abertos ficam sem efeito, e as folgas e os blocos de reserva
  depois do dia saem do calendário. O que já foi voado não muda. A posição fica **memorizada**.
* **Gerar na base nova:** escolha a base e marque **Começar de onde o avião está**. A escala nova começa no dia seguinte
  ao último voo, saindo de onde o avião ficou. O planejador do AeroScale começa fora da base, levando em conta a jornada
  e o repouso dos dias anteriores, e leva você para a base nova com voos reais (ou por terra, entre aeroportos da mesma
  cidade, como GRU e CGH).
* **Desfazer:** enquanto você não gerar a escala nova, dá para trazer a escala apagada de volta.
* **Gerar a partir do dia escolhido:** no Mês (e em 6 meses ou ano), o campo **A partir do dia** faz a escala começar nesse
  dia em vez do dia 1. Com **Substituir a escala que já existe nesses dias** (marcado por padrão), o que vem antes do dia
  fica, o que existe do dia até o fim do período é refeito, e a escala nova continua de onde o avião estava no último voo
  antes do dia (repouso e jornada contam). A escala antiga só sai quando a nova está pronta. Se a escala que já existia
  depois do período não começar onde a nova termina, a tela avisa.
* Se o AeroScale mandar a escala antiga de novo, o que foi apagado continua fora.

## 2.1: pedido de troca de escala e backup completo

Também na 2.1: o backup passou a levar a LineOps inteira, sai sozinho antes de cada atualização, e dá para restaurar pela
tela. Veja [Backup e restauração](#backup-e-restauração).

Na Escala, abra um dia e use **Pedir troca** numa perna. Dá para pedir duas coisas:

* **Trocar por outro voo no mesmo trecho**, no mesmo dia, ou trocar o bate-volta inteiro por outro par de voos. As opções
  são só voos do RPL da companhia (o mesmo do motor do AeroScale); voo de dia sem RPL publicado aparece como PROJETADO.
* **Escolher o destino**, em qualquer perna: você vai para o destino escolhido e de lá segue para onde a sua escala
  continua (ou, se a perna fecha um ciclo, faz uma ida e volta para lá no lugar do ciclo). Pode ficar até 3 dias no destino,
  se jornada e repouso deixarem. Se não houver voo direto do destino para onde a escala continua, saem também as pernas
  seguintes (até 4), até um ponto para onde dá para voltar direto: GIG→VIX + VIX→GRU vira GIG→REC + REC→GRU.
  A lista só mostra destinos com pelo menos uma opção que cabe de verdade; dá para **digitar** qualquer aeroporto (ICAO ou
  IATA) e a LineOps diz se dá e, se não der, exatamente por quê (sem voo da companhia no RPL, sem volta a tempo, limite de
  jornada).
* **Sair da perna**, sempre: se as pernas fecham um ciclo (saem e voltam ao mesmo aeroporto), saem juntas. Em qualquer perna,
  dá também para sair do voo e ir de **extra** (passageiro, sem voar) até onde a escala continua, no próprio voo ou em
  outro voo real do mesmo trecho.

O único limite é o de fadiga: se a opção passa da jornada ou do repouso (ou cria outra violação das regras de tripulação),
ela aparece desativada com o motivo. Fora isso, sempre dá para pedir, mesmo com chance baixa.

Antes de virar pedido, cada opção passa pelas regras de verdade: conexão mínima de troca de aeronave (`swap_min` do
AeroScale, 50 min) e jornada, repouso, madrugadas e limites do mês pelo `crew.analyze` do AeroScale. O que não cabe aparece
riscado, com o motivo (ex.: "Repouso de 9,8 h antes da jornada; mínimo 12 h (Lei 13.475 art. 48)").

**Chance de aprovação.** É uma regra de jogo da LineOps, mostrada fator a fator antes de enviar:

| Fator | Efeito |
|---|---|
| Base | troca 75 · sair da perna 55 |
| Antecedência | 7 dias ou mais +10 · 3 a 7 dias 0 · menos de 72 h −15 · menos de 24 h −35 |
| Pedidos nos últimos 30 dias | −7 cada (até −35) |
| Sexta ou domingo (dia de pico) | −8 |
| Mais de uma etapa | −4 por etapa a mais na troca · −6 na saída |
| Destino escolhido por você | −10 |
| Cai num dia de folga programada | −15 |
| Voo PROJETADO | −10 |
| Pontualidade (10+ voos medidos) | OTP15 ≥ 85% +5 · < 60% −5 |

O total fica entre 5% e 95%. Não é estatística de nenhuma companhia.

**A resposta chega em minutos**, como mensagem da equipe (e no celular, se estiver ligado). Com 3 dias ou mais de
antecedência, quem responde é o Planejamento de Escala, em 3 a 12 min. Com menos, é o Controle de Tripulantes, em 1 a 6 min.
Aprovado, a escala muda na hora, com histórico, e a reserva do voo antigo é cancelada. Negado, a mensagem diz o motivo que
mais pesou. Se a apresentação passar antes da resposta, o pedido fica sem efeito. Se o AeroScale mandar a mesma escala de
novo, a troca continua valendo; se ele mudar aquela perna, vale a escala nova. Os pedidos ficam listados na Carreira.

## 2.0.2

* Corrige o erro 500 no login da 2.0.1 (`'list' object has no attribute 'mark_used'`): uma variável dentro do servidor
  tinha o mesmo nome de um módulo. Um teste novo impede isso de voltar.
* **`recopiar-golops.bat`**: se você registrou voos no GolOps antigo depois que a LineOps copiou os dados dele, ele traz
  os dados de novo. A pasta atual da LineOps não é apagada: vai inteira para `dados-substituidos-…` ao lado.
* O aviso "o GolOps foi usado depois da cópia" agora diz as datas (da cópia e da última mudança no GolOps) e o motivo de
  a LineOps contar como usada.

## 2.0.1: abre a LineOps, não o GolOps antigo

Na 2.0.0, quem tinha o GolOps iniciando com o Windows abria a LineOps e via o GolOps: o antigo continuava na bandeja
ocupando a porta 8787, a LineOps não conseguia subir e o navegador mostrava o que já estava lá. Agora:

* **Antes de subir, a LineOps pergunta quem está na porta.** Se for o GolOps antigo, ela fecha ele (o processo que
  escuta a porta e qualquer `GolOps.exe`) e assume. Se não conseguir, explica o que fazer, em vez de abrir a tela errada.
* O "iniciar com o Windows" do GolOps sai (no app instalado, vira o da LineOps), para ele não voltar no próximo boot.
* O **instalador** fecha o GolOps antes de instalar.
* O `iniciar.bat` só abre o navegador quando quem responde é a LineOps.
* **Dados:** a cópia dos dados do GolOps só acontece com ele fechado. Se a 2.0.0 copiou com o GolOps aberto e a LineOps
  nunca chegou a ser usada, a cópia é refeita com os dados mais novos; a anterior fica inteira em
  `%LOCALAPPDATA%\LineOps\dados-substituidos-…`. Se a LineOps já foi usada, nada é trocado.
* A tela e os scripts são sempre revalidados (e levam a versão na URL): depois de atualizar, o navegador não mostra mais
  a versão antiga guardada no cache.

## 2.0: LineOps
* **Modos por companhia.** Depois do login você escolhe a companhia. Cada carreira tem a sua escala, frota, voos, diário,
  salário, equipe, tripulação, cheque e marcos, num banco próprio (`dados/carreiras/GLO.db`). O piloto, o celular, os Voos
  Externos e os ajustes comuns (VID da IVAO, Pilot ID do SimBrief) ficam no núcleo (`dados/lineops.db`) e valem para todas.
  **GOL** está pronta; **LATAM**, **Azul** e **companhias do mundo** aparecem como "em breve" (a arquitetura já recebe).
  Troca de companhia pela barra lateral.
* **Cores da companhia** em toda a tela. **Logo:** cada carreira tem um espaço para você pôr o arquivo de logo que quiser
  (PNG, JPG, WEBP ou SVG). A LineOps não traz logotipo de companhia nenhuma; sem arquivo, aparece um selo com o nome.
* **O AeroScale agora mora dentro.** O motor do AeroScale 2.2.4 (cópia literal: leitor de RPL do CGNA, atualização semanal
  do RPL, gerador de escalas, regras de tripulação, rodízio do ADS-B, reservas, internacionais, importação de escala real)
  roda na própria LineOps. **Escala → Gerar escala:**
  * escala do tripulante (troca de aeronave nos hubs, extra em voo real), com o **perfil calibrado com escalas reais de
    comandante** do AeroScale 2.2 (pernas por dia, sobreaviso, folga em bloco, INATIVO fora da base);
  * mês, semana, período de até 62 dias, 6 meses ou ano; base e habilitação vêm do que o RPL da companhia tem de verdade;
  * o seu pedido: folgas em dias escolhidos, evitar madrugada, preferir manhã ou tarde, blocos de reserva; no avançado,
    sobreaviso, internacionais, chance de chaves de 2 e 3 etapas, aeroportos preferidos e evitados, semente;
  * Lei 13.475 e RBAC 117 aplicados pelo próprio motor; o resultado aparece com os diagnósticos dele;
  * regerar o mesmo período **substitui** a escala, e a equipe avisa a alteração como numa companhia de verdade;
  * **nada inventado:** dias fora da semana de RPL publicada repetem a semana mais nova (ou a mesma semana do ano anterior)
    e vêm marcados como PROJETADO; o RPL novo chega sozinho;
  * **Importar escala real (texto)**, como circula entre tripulantes: o motor casa cada voo com o RPL.
  Importar backup do app do AeroScale continua valendo (só entram as escalas da companhia da carreira).
* **Backup de tudo** (núcleo + carreiras) num .zip com assinatura de cada arquivo; backups antigos do GolOps voltam como a
  carreira GOL.
* **Atualizando do GolOps:** nada a fazer. Na primeira abertura a LineOps copia `%LOCALAPPDATA%\GolOps\dados`, guarda uma
  cópia do banco antigo em `backups\`, transforma o banco na carreira GOL e monta o núcleo. O instalador usa o mesmo
  identificador, então atualiza o GolOps instalado (e tira o executável e os atalhos antigos). Quem tinha "Iniciar com o
  Windows" continua com ele. Você precisa entrar de novo (o cookie de sessão mudou de nome).

## 1.1: Voos Externos (fora das OPS)
Uma aba para quando você não está na GOL: **long haul de verdade para voar com os amigos**.
* **Buscar voos no ar agora**: o LineOps pergunta ao **adsb.lol** (API pública, dados ODbL) quais long-haulers estão voando
  (777, 787, A330, A350, A340, 747, A380, MD-11, 767) e a rota de cada callsign. Só entram rotas plausíveis, de um trecho,
  com 5.500 km ou mais. Cada busca alimenta um catálogo local; filtre por aeronave, origem/destino (ICAO, país ou prefixo)
  e duração, ou clique em **Sortear**.
* **Voo em grupo**: horário do grupo em UTC, saídas escalonadas (padrão 3 min), cada amigo com nome, rede (IVAO/VATSIM),
  callsign (sugerido a partir do real: QTR773, QTR773B…) e tipo. O LineOps gera o **convite** para colar no WhatsApp/Discord
  e o link do **SimBrief** de cada um.
* **No dia**: **radar do grupo** (cada callsign procurado no whazzup da IVAO e no data feed da VATSIM, com progresso e
  distância que falta) e o tempo nas duas pontas.
* **Depois**: cada um lança bloco e toque; sai o **pódio** do voo e o ranking **"quem pousa melhor"** do grupo.
* Estimativas são marcadas como estimativa: a duração usa o cruzeiro típico do tipo (+5% de rota, sem vento; o SimBrief
  calcula o real). Nada disso mexe na escala, no diário, na carreira ou nos salários da GOL.

## 1.0
* **Celular** (Ajustes → Celular): a operação te avisa pelo **Telegram** (bot seu, de mão dupla: botões de ciente, assinar
  o despacho e código de atraso; comandos `/hoje`, `/proximo`, `/pernoite`, `/mensagens`) ou pelo **ntfy** (sem conta).
  Só o importante por padrão, com horário de silêncio (o despertar sempre toca). Ver "Celular" abaixo.
* **Tripulação do dia**: copiloto e comissários (fictícios) que seguem juntos a viagem. Briefing na apresentação, a chefe de
  cabine no interfone, o comentário do copiloto depois do toque e o "até amanhã no lobby".
* **Carreira** (nova tela): pontualidade OTP15 com código de atraso IATA, marcos (primeiro pouso em cada aeroporto, horas,
  pousos, sequência de toques suaves), cheque de rota anual avaliado com os dados reais do voo e "com quem você mais voou".
* **Boletim da semana** da chefia, toda segunda de manhã.
* **Durabilidade**: backup automático diário (guarda os 30 mais recentes) e atalho de app na tela inicial do celular.

