# LineOps

Centro de operações **pessoal** e **autohospedado** de simulação aérea, com **um modo por companhia** (projeto que nasceu
como GolOps). Projeto independente de simulação, **sem afiliação, patrocínio ou endosso de nenhuma companhia
aérea**. Todo conteúdo é simulado e não é documentação operacional.

```
RPL do CGNA (AZU, GLO, TAM) ─▶ motor do AeroScale 2.2.4 ─▶ carreira (GOL hoje) ◀──HTTP── LineOps ACARS (PC do simulador)
  embutido + atualizado         escala do tripulante        escala, frota, voos,        X-Plane 12 / MSFS / P3D
  toda semana                   (dentro da LineOps)          diário, salário, equipe
```

## O que é

* **Escala do tripulante** gerada pelo motor do AeroScale com o RPL do CGNA (GOL, LATAM, Azul), com folgas, reserva,
  sobreaviso, pernoite pedido, continuações do avião, mínimo de pernas por dia e "Mais voos" a partir de um dia.
  Ou importada do texto que circula entre tripulantes.
* **Voo a voo**: reserva com matrícula, OFP do SimBrief, loadsheet, METAR/TAF, ACARS (IVAO ou cliente próprio para
  X-Plane 12, MSFS e P3D), PIREP com o toque (fpm e G) e diário de bordo.
* **Frota por matrícula** com configuração, disponibilidade calculada, discrepâncias e **MEL** (importada do seu PDF).
* **Biblioteca técnica**: os seus PDF (MEL, CDL, QRH) pesquisáveis, só na sua instalação.
* **Equipe e rede**: CCO, controle de tripulantes, tripulação do dia (fictícia), trocas de escala, pernoite com hotel,
  aviso no Discord, outros pilotos da companhia na IVAO.
* **Carreira, salários e estatísticas.**

O que mudou em cada versão está no [CHANGELOG.md](CHANGELOG.md).

## Instalar como app do Windows (recomendado)
1. Baixe o **`LineOps-Setup-<versão>.exe`** em [Releases](../../releases) e rode. O instalador cria atalhos no menu Iniciar (LineOps e LineOps ACARS) e, se você marcar,
   inicia com o Windows e libera o acesso pela rede de casa no firewall (só em redes privadas).
   Sem assinatura digital, o Windows pode mostrar “O Windows protegeu o computador”: **Mais informações → Executar assim mesmo**.
2. Abra o **LineOps**: ele abre numa janela própria e fica com um ícone na **bandeja**, perto do relógio. Fechar a janela não
   para o LineOps (o acompanhamento pela IVAO e a equipe seguem trabalhando); para parar, **Sair do LineOps** no menu do ícone.
3. Na primeira vez, um assistente pede o backup do AeroScale, a frota (CSV), o VID da IVAO e o Pilot ID do SimBrief.
4. Para o PC do simulador e o celular: ícone da bandeja → **Acesso pela rede**, que mostra o endereço (ex.: `http://192.168.0.10:8787`).
5. No PC do simulador, **LineOps ACARS**: dois cliques abrem o modo guiado (servidor, token, simulador).

Os dados ficam em `%LOCALAPPDATA%\LineOps\dados` e **não são apagados** ao desinstalar ou atualizar. Antes de cada atualização
do banco, o LineOps faz um backup sozinho. O log fica em `dados\logs\lineops.log` (Ajustes → Sobre, ou pelo menu do ícone).
Quem usava a versão por `.bat` não perde nada: na primeira execução, o app copia a pasta `dados` antiga.

### Gerar o instalador (para quem desenvolve)
No Windows, com Python 3.10+ e o [Inno Setup 6](https://jrsoftware.org/isinfo.php): dois cliques em **`packaging\build.bat`**.
Ele cria um ambiente separado, roda os testes (se falharem, para), empacota com PyInstaller (`dist\LineOps\LineOps.exe`,
`dist\LineOps ACARS\LineOps ACARS.exe`) e gera `dist\LineOps-Setup-<versão>.exe`. Para rodar o app a partir do código:
`pip install -r requirements-desktop.txt` e `python -m lineops desktop`.

## Rodar pelo código (desenvolvimento, sem instalador)
Requisito: **Python 3.10+**. Dois cliques em **`iniciar.bat`** e pronto: na primeira vez ele cria o ambiente em
`%LOCALAPPDATA%\LineOps\venv`; nas outras, inclusive com **uma versão nova extraída em outra pasta**, ele só confere
e sobe em segundos. Só instala alguma coisa quando a versão nova trouxer pacote novo (ele compara os requirements).
Os dados também são os mesmos para todas as versões (`%LOCALAPPDATA%\LineOps\dados`, igual ao app instalado); a pasta
`dados` antiga, se existir ao lado, é copiada uma vez. `iniciar.bat app` abre como app (janela + bandeja),
`iniciar.bat rede` libera para o PC do simulador. O `build.bat` reaproveita do mesmo jeito o `venv-build`.

### Modo antigo (referência)
Requisito: **Python 3.10+** ([python.org](https://www.python.org/downloads/), marcando "Add python.exe to PATH").

1. Dê dois cliques em **`instalar.bat`** (cria `.venv`, instala as dependências; precisa de internet só nessa hora).
2. Dê dois cliques em **`iniciar.bat`**: o servidor sobe e o navegador abre em `http://127.0.0.1:8787`. Os dados ficam na pasta `dados\`.
   No primeiro acesso você cria seu usuário. Para outro PC (o do simulador) acessar: **`iniciar.bat rede`** (libere o firewall para Redes Privadas).
3. No site: **Ajustes → Importar** o backup `.zip` do AeroScale (há prévia que não grava nada) e depois **Frota** (cadastre as matrículas reais que você usa; nada é inventado).
4. **Escala → Reservar** uma perna e escolher a aeronave. Em **Ajustes → Tokens do ACARS** gere um token.
5. No PC do simulador, copie a pasta **`acars\`** e dê dois cliques em **`iniciar-acars.bat`**: ele pergunta servidor, token e simulador (X-Plane 12, MSFS 2024 ou Prepar3D v5), guarda em `acars.cfg`, lista seus voos e inicia o ACARS. Só precisa de Python (sem instalar pacotes).
6. Ao acionar o calço no destino, o ACARS envia o **PIREP**. Revise em **PIREPs**; só ao **aprovar** entram diário, horas e ciclos.
7. **`backup.bat`** cria uma cópia do banco em `dados\backups`. Guarde fora deste PC.

Linha de comando (Linux/macOS ou avançado): `pip install -r requirements.txt`, `python -m lineops --data ./dados serve [--lan]`,
`python -m lineops_acars --server URL --token TOKEN --sim xplane|msfs|p3d|replay --flight ID`.

## Celular
**Por que não notificação do navegador?** Web Push exige HTTPS com certificado válido; o LineOps roda na sua rede em
`http://`. Adicionar à tela inicial funciona como atalho (com ícone e nome), mas os avisos vêm do Telegram ou do ntfy.

**Telegram (recomendado):**
1. No Telegram, abra o **@BotFather**, mande `/newbot`, escolha um nome e um usuário terminado em `bot`.
2. Cole o token em Ajustes → Celular → Conectar o bot.
3. Abra o seu bot e mande `/start CÓDIGO` (o código de 6 dígitos que aparece na tela). Só esse chat passa a falar com o LineOps.

O LineOps conversa com a API do Telegram por HTTPS (long polling): nenhuma porta é aberta na sua rede.

**ntfy:** instale o app ntfy, clique em "Criar meu tópico" no LineOps e assine o tópico no app. O nome do tópico funciona
como senha (no ntfy.sh, tópicos são públicos para quem souber o nome).

**Links fora de casa:** as notificações chegam de qualquer lugar; os links "Abrir" só abrem se o celular alcançar o PC
(mesma Wi-Fi ou uma VPN como o Tailscale). Informe o endereço do PC em "Endereço do LineOps para os links".

## O que tem dentro
**Avisos de validade** — o LineOps avisa no Painel quando algo que ele usa venceu ou está para vencer: a CCT dos aeronautas
(30/11/2026), as tabelas de INSS e IRRF do ano, a versão das regras do AeroScale e o backup (mais de 7 dias sem backup).

**Painel** — a abertura é a sua jornada: saudação, data, próximo voo em destaque (com rota desenhada, horários UTC e locais,
aeronave e situação técnica), o trilho cronológico do dia (apresentação, pernas, tempo de solo, encerramento, pernoite) e o
resumo operacional. Sem voo no dia, o painel diz quando e onde é a próxima apresentação; em dia de reserva, mostra o período e o acionamento.

**Escala** — vista de **dia** (trilho da jornada), de **semana** (sete linhas que abrem com as pernas de cada dia) e de
**tripulação** (jornadas, limites RBAC 117, repousos, folgas e violações, calculados pelo `crew.analyze` do AeroScale).
Cada perna mostra a origem do dado do AeroScale (REAL, PROJETADO, OBSERVADO…) e permite reservar escolhendo a aeronave.

**Despacho** — no Flight Center, o botão **Importar OFP** busca o seu último plano na API pública do SimBrief (só precisa do
Pilot ID, guardado em Ajustes) e guarda o plano com o voo: combustível, pesos com margem para cada limite, tempos, rota e
alternado. Sem internet no servidor, dá para enviar o JSON do plano como arquivo. Depois do voo, o PIREP mostra
**planejado × medido**. A **meteorologia** (METAR e TAF do aviationweather.gov) entra na mesma tela pelo módulo do AeroScale.

**LOADSHEET** — calcula DOW, payload, ZFW, TOW e LW a partir do OFP e dos limites da sua frota, com a margem de cada
limite e **a fonte de cada número** (OFP, sua frota, ou informado por você na emissão). Emite em edições numeradas e gera
PDF marcado como simulação. Acima de um limite, só emite com justificativa.

**Flight Center** — a página de cada voo, com as dez etapas da preparação e o estado de cada uma: voo e horários, aeronave,
situação técnica (MEL simulado), despacho (SimBrief montado pelo módulo do AeroScale, com rota, PBN e alternado sugerido), rota e nível do RPL, LOADSHEET, meteorologia,
briefing, ACARS (com o comando pronto para copiar) e PIREP.

**IVAO como ACARS (provisório)** — com o seu VID em **Ajustes → IVAO**, o botão **Acompanhar pela IVAO** no Flight Center
faz o servidor ler o whazzup público da IVAO (o mesmo dado do [Webeye](https://webeye.ivao.aero)) a cada 15 s, achar você
pelo VID e gravar a trilha. OUT, OFF, ON e IN saem do que a rede publica (no solo andando, no ar, de volta ao solo, parado
duas leituras seguidas ou desconectado depois do pouso), com precisão de ±15 s; no IN o PIREP vai sozinho para a sua
revisão, com link do Webeye. A IVAO não publica combustível, razão no toque nem força G: esses campos ficam em branco.
Sem instalar nada no PC do simulador.

**Mensagens: a equipe da operação** — o LineOps deixou de ser só um painel: a companhia fala com você, setor por setor,
como numa empresa aérea brasileira de verdade. As pessoas são **personagens fictícios** (nomes editáveis em
Ajustes → Equipe da operação), com turnos manhã/tarde/noite pela hora local da sua base; o **conteúdo** de cada mensagem
vem de fatos do LineOps, nunca inventado.

| Setor | O que manda | Base real |
| --- | --- | --- |
| Planejamento de Escala | escala publicada ou atualizada pelo AeroScale, com resumo e conferência dos 5 dias de antecedência (pede ciente) | Lei 13.475, art. 26 |
| Controle de Tripulantes (CCO) | alteração de escala com o antes e depois (pede ciente), confirmação de reserva da perna, lembrete de apresentação na véspera, reserva, acionamento (pede ciente) e sobreaviso | arts. 26, 43 e 44 |
| Despacho Operacional (DOV) | 3 h antes, pede o plano com o link do SimBrief; com o Pilot ID salvo, **busca o OFP sozinho e anexa**; manda o briefing (rota, nível, combustível, alternado, pesos, METAR em cache, MEL, aviso de NOTAM) e **libera ou retém** o despacho; acompanha o voo pela IVAO (decolagem, pouso, perda de sinal) | despacho conjunto, RBAC 121.663 |
| Controle de Carregamento | LOADSHEET emitido, com margens; nova edição como LMC | — |
| Controle de Manutenção (MCC) | situação da aeronave designada, discrepâncias abertas e fechadas | MEL simulado |
| Chefia de Equipamento | voo aprovado ou declarado; toque abaixo de −600 fpm vira conversa de segurança, sem punição | — |
| Folha de Pagamento | fechamento do mês (com salários ligados) | — |

O **despacho conjunto** aparece no Flight Center: o DOV assina primeiro (ou retém, dizendo o motivo, como OFP de outro
trecho, aeronave indisponível ou peso acima do limite) e a etapa só fica pronta com a **sua assinatura**. Começar o voo
sem ela gera um alerta do DOV. Mensagens novas aparecem como aviso na tela, com contador no menu.

**Toque e Chefia de Manutenção** — todo fim de voo pede a **razão (fpm) e o G do toque**: no PIREP do ACARS vêm medidos (dá
para corrigir), no PIREP da IVAO e no “já fiz esse voo” você informa o que o simulador ou o add-on mostrou. Sem isso o voo
não entra no diário. Os critérios são os reais do 737NG:
- limite de projeto do trem: **600 fpm** (10 ft/s) no peso máximo de pouso e **360 fpm** (6 ft/s) no de decolagem;
- AMM 05-51: inspeção de pouso duro acima de **2,2 G** de pico; Fase 1 visual, Fase 2 só se a Fase 1 achar dano;
- o relato da tripulação é, para a Boeing, o critério mais confiável: marcar “reportei pouso duro” manda para inspeção;
- pouso acima do MLW (com o MLW cadastrado na frota) também pede inspeção.

Pouso duro **bloqueia a aeronave** com um item “Inspeção de pouso duro (AMM 05-51)” até você fechá-lo na Frota, o
Eng. Walter Bastos (Chefia de Manutenção, personagem fictício) manda a bronca e o Chefe de Equipamento te convoca.
Toque firme leva um puxão de orelha; pouso manteiga leva o lembrete de que flare longo come pista. Em Ajustes dá para
trocar o tom: **folclórico** (a bronca de hangar) ou **just culture** (o que a Boeing recomenda: aprender, não punir).
O Diário mostra o toque de cada voo, a média, o melhor e quantos pousos duros você tem.

**Rede** — os outros pilotos da companhia na IVAO, não só o número:
- **Ao vivo:** um cartão por voo com callsign, VID, rating, avião e simulador, rota com cidades, fase, nível, velocidade,
  barra de progresso e ETA (os dois estimados pela distância e velocidade publicadas) e há quanto tempo está online. Um
  **radar** desenha todos sobre os aeroportos do AeroScale, com o tracejado até o destino. Filtros: no ar, no solo, colegas.
- **Ligação com a sua escala:** cada cartão diz se o piloto vai para o mesmo destino que você, está no solo no mesmo
  aeroporto, voa a sua próxima perna, chega de onde ela sai ou passa pela sua base.
- **Colegas:** marque pilotos com a estrela e dê um apelido (a IVAO não publica nomes; o apelido fica só no seu LineOps).
  Quando um colega entra na rede, aparece um aviso no LineOps.
- **Ao longo do tempo:** com o VID salvo, o servidor anota a cada 5 min quem da companhia está online (90 dias). Daí
  saem “quem você mais encontra”, o histórico de cada VID (dias, rotas, chegadas) e **quem estava na rede junto com você**.

**Já fiz esse voo** — no Painel, na Escala e no Flight Center. Para o voo feito offline, em outra rede ou sem ACARS: você
informa OUT e IN (OFF, ON, combustível e razão no toque são opcionais) e ele entra no diário marcado como **declarado**,
com horas e ciclos na aeronave. Os salários (realizado) contam o voo normalmente. O estorno marca, não apaga.

**Centro de operações e rede ao vivo** — o Painel cumprimenta pela hora do dia, mostra o relógio UTC e local, as mensagens
do CCO (o que aconteceu na sua operação: reservas, OFP, LOADSHEET, ACARS, PIREPs, escala nova) e **quem está voando agora**:
voos da companhia na IVAO, controle online nos seus aeroportos e o movimento de cada um. O Flight Center ganhou a mesma
leitura para origem e destino (“Na frequência”) e a linha do tempo do voo. Tudo vem de eventos reais e do whazzup; sem
internet no servidor, o painel diz que a IVAO está fora do ar em vez de inventar tráfego.

**Mensagens em rota (ACARS)** — com o voo acompanhado (IVAO ou cliente ACARS), a operação fala com você conforme o avião
avança, e o Flight Center mostra cada mensagem como uma impressão do ACARS, com o chime da cabine (desliga em Ajustes):
* subida: a **cabine** (chefe de cabine, interfone) começa o serviço; com mais de 3h30 de jornada, manda a refeição da
  tripulação (alimentação em voo a cada 4 h no máximo, Lei 13.475, art. 61 §3);
* ~25% do voo: o **Controle de Tripulantes** manda o que vem depois do pouso: a **próxima perna** (solo, aeronave, despacho)
  ou o **pernoite**: fim da jornada (30 min após o corte, art. 35 §4), repouso mínimo (12/16/24 h, art. 48) e se o repouso
  programado até a próxima apresentação cumpre a lei, hotel e transporte (do cadastro de **Pernoites** em Ajustes; sem cadastro,
  a mensagem diz que falta e lembra o art. 47), refeições da CCT que caem no pernoite, pôr e nascer do sol, e quem da companhia
  também está indo para lá na IVAO;
* ~55% do voo ou 40 min do pouso: o **DOV** manda o METAR e o TAF do destino e do alternado, lidos na hora;
* ~70% do voo: o **MCC** diz o que a aeronave faz depois do pouso e os itens abertos;
* no calço: boas-vindas e a hora em que o repouso começa de verdade.
O progresso vem da posição (distância até o destino) ou, sem posição, do tempo desde a decolagem contra o OFP. Num voo
declarado no "Já fiz esse voo", o pacote do pernoite chega logo depois do lançamento.

**Pernoite imersivo** — em **Ajustes → Pernoites**, você busca os hotéis perto de cada aeroporto no **OpenStreetMap**
(Overpass, sem chave) e escolhe um; o LineOps calcula a rota de carro pelo **OSRM** e guarda. A partir daí cada pernoite
tem uma linha do tempo: fim da jornada (calço + 30 min, Lei 13.475, art. 35 §4) → carro no desembarque → check-in (rota +
10 min) → despertar (saída − 60 min) → carro na portaria (apresentação − rota − 15 min) → apresentação. Folga e
antecedência são ajustáveis; transporte em **van da tripulação** (padrão; o art. 47 manda a empresa fornecer) ou **carro
por aplicativo**. As mensagens chegam **na hora real**, com aviso na bandeja do Windows: a van no desembarque (veículo,
placa e motorista fictícios), o check-in da Hotelaria, o despertar e o carro na portaria. Em voo acompanhado, os horários
são reagendados pelo calço real. Se a escala mudar ou o voo for estornado, o que estava agendado é cancelado; o que
passou da hora há mais de 3 h (LineOps fechado) não chega atrasado. O Flight Center mostra o cartão do pernoite com o
status de cada etapa. O LineOps diz "hotel escolhido por você": não sabe (nem finge saber) qual hotel a GOL usa.

**Ignorar o horário da escala** — no "Já fiz esse voo", para quem voa uma perna antes (ou depois) da hora programada:
*usar o horário em que voei* (o diário guarda a partida programada ao lado) ou *registrar nos horários da escala* (conta no
dia programado para salário e escala, mesmo no futuro; o diário marca "horário da escala"). Sem a opção, OUT no futuro
continua recusado. O lembrete de apresentação não lembra de um dia que você já voou.

**ACARS** — repete sozinho quando a rede oscila, manda batimento enquanto o simulador está pausado, retoma a sessão se o
cliente reiniciar e avisa na tela quando a sessão fica sem sinal.

**Salários (opcional, desligado por padrão)** — liga em **Ajustes → Salários**. Monta um contracheque simulado do mês com
os valores da **CCT 2025/2026 da aviação regular (SNA × SNEA)**, cada um com a sua cláusula: parte fixa (salário base +
compensação orgânica de 20%, cl. 2.2 e 3.2.3), garantia de 54 h com reserva paga como hora de voo e sobreaviso a 1/3
(cl. 3.2.7), hora acima da garantia (cl. 3.2.8), horas em domingos e feriados da base em dobro (cl. 3.2.6), DSR sobre a
parte variável com 8 folgas (cl. 3.4.8) e diárias de alimentação nas janelas da cl. 2.3 (nacionais em R$; internacionais
na moeda da CCT, sem conversão). INSS e IRRF de 2026 entram como estimativa (Portaria MPS/MF nº 13/2026, tabela mensal
e redutor da Lei 15.270/2025). Duas bases: **programado** (pernas da escala do AeroScale) e **realizado** (voos aprovados
no diário). As jornadas que definem as diárias e o café no hotel depois de pernoite fora da base saem do `crew.analyze` do
AeroScale; a função e a categoria da aeronave são sugeridas pelo perfil de carreira e pelo tipo da escala. O contrato é
versionado por mês (um reajuste não reescreve meses passados); valores que você mudar ficam marcados como seus. No fim do
mês dá para **fechar** o realizado (foto imutável, anulável com motivo), baixar o PDF e ver o resumo do ano.

**Frota, PIREPs, Diário e Ajustes** — ficha por matrícula com limites, discrepâncias, indisponibilidades, histórico e livro-razão
de horas; revisão de PIREP com trilha e mensagens; diário de bordo; importação, tokens e backups.

### Interface
Tema escuro grafite com laranja da identidade GOL, tipografia de sinalização aeroportuária (Bahnschrift/DIN no Windows, com
alternativas em outros sistemas) e dados em fonte monoespaçada, como num OFP. As animações são de entrada escalonada, desenho
da rota, contagem de números, expansão de seções e transição entre telas; todas respeitam `prefers-reduced-motion`.
Nada é carregado da internet (sem CDN, sem fontes externas): o LineOps funciona numa rede local sem acesso externo.

Horas locais nos aeroportos usam o fuso de cada um. No Windows isso depende do pacote `tzdata`, já incluído em `requirements.txt`;
sem ele, o LineOps mostra apenas UTC em vez de chutar.

## As regras são as do AeroScale, não uma reescrita
A pasta `lineops/aeroscale/` contém o **motor do AeroScale 2.2.4 em cópia literal** (a única mudança: em `db.py` o import
do Flask é opcional). A LineOps não reimplementa nada: gera a escala com o `scheduler.py` dele (`generate`, no modo
tripulante), lê e atualiza o RPL com `rpl_service.py`/`rpl_update.py`, importa escala real com `roster_import.py` e aplica
as regras abaixo com as mesmas funções. Para atualizar o motor, copie os arquivos novos do AeroScale por cima da pasta
(e os RPL em `aeroscale/rpl/`). O banco do motor fica em `dados/motor/aeroscale.db`, com o esquema do próprio AeroScale.

| Módulo copiado | O que passa a valer no LineOps |
| --- | --- |
| `crew.py`, `crew_profiles.py` | jornada, repouso, folgas, madrugadas e limites (RBAC 117 tab. B.1 / Lei 13.475), códigos e perfil de cada companhia |
| `predict.py` | classificação de solo e pernoite (`gap_kind`) e o modelo de rodízio |
| `catalog.py`, `airports.py`, `airport_geo*.py`, `world_tz.py` | aeroportos, coordenadas, pistas e **fusos**, inclusive dos estrangeiros |
| `ops.py` + `ops_observed.json` | pernoites e rodízio observados (RPL e ADS-B) |
| `delays.py` + `delays_observed.json` | pontualidade observada |
| `intl.py` + `intl_observed.json` | voos e aeroportos internacionais |
| `simbrief.py` | link do SimBrief com rota e PBN do RPL e **alternado sugerido** com justificativa |
| `reserve.py`, `career.py`, `logbook.py`, `rpl_parser.py`, `rpl_service.py`, `weather.py` | reservas, carreira, diário, leitura de RPL e meteorologia |

Como esses módulos consultam o banco do AeroScale, **cada importação guarda uma cópia somente-leitura dele** em
`dados/aeroscale/source.db`, e é contra ela que as regras rodam. Sem a cópia, o que depende de dados embutidos
(aeroportos, tipos, tripulação) continua valendo; o que depende do RPL fica indisponível e a tela diz isso.

**O que não foi migrado, de propósito:** o gerador de escala (`scheduler.py`), a coleta de RPL e de ADS-B e a interface
do AeroScale. Quem monta a escala é o AeroScale; o LineOps só lê — é a regra do projeto.

**Atualizar:** quando você mexer nas regras do AeroScale, copie os arquivos novos por cima de `lineops/aeroscale/`.
O LineOps compara a versão do backup com a dos módulos copiados e avisa na importação quando elas divergem.

## Regras aplicadas na leitura da escala
O LineOps não inventa regra de escala: as que valem são as do AeroScale, portadas com os mesmos limiares.
- **Dia da escala**: a perna pertence ao dia da sua **partida em hora local** (`local_date`, como em `api.leg_brief`), não à data UTC.
  Por isso um voo que parte 02:50Z aparece no dia anterior quando lá são 23:50.
- **Solo e pernoite**: classificação idêntica à de `predict.gap_kind` (solo, solo curto, solo longo, pernoite, parada noturna curta,
  parada longa), em hora local do aeroporto. Conta como pernoite o que o AeroScale conta: essas classificações **ou** 6 h ou mais no solo.
  Há um teste que compara a lista de pernoites derivada com o diagnóstico "Pernoites: …" que o próprio AeroScale gravou no backup.
- **Apresentação e encerramento**: dos parâmetros da sua escala (`report_before`, `release_after`).
- **Folgas**: de `planned_days_off`. **Reservas e acionamentos**: da tabela de reservas. **Diagnósticos** do AeroScale são importados e exibidos.
- **Tripulação**: `crew.analyze` do AeroScale, na aba Tripulação da Escala (jornadas, limites, repousos, folgas, violações).
- **Fusos estrangeiros**: resolvidos pelo `world_tz.py` do AeroScale (Buenos Aires, Santiago, Miami…), não por palpite.

## Dados e honestidade
- Valores desconhecidos ficam em branco (nunca zero): limites de peso, configuração e horas base só existem se você informar.
- `data_origin` do AeroScale (REAL, PROJETADO, OBSERVADO…) é mostrado como veio.
- Medições do ACARS são rotuladas como medições do simulador. Voo noturno = sol abaixo de −6° (aproximação astronômica).
- Disponibilidade da aeronave é **calculada** (indisponibilidades, discrepâncias, diferimentos vencidos) — nunca digitada.
- Horas/ciclos são um **livro-razão**; estorno marca o lançamento, não apaga.

## Privacidade e o que sai do seu PC
- **Estatística das escalas geradas** (só no instalador oficial): cada escala gerada manda um resumo anônimo para o
  Discord do projeto (companhia, tipo, base, período, pernas, horas, aeroportos e rotas mais usados, versão e um código
  aleatório da instalação). Não vai nome de usuário, e-mail, IP nem senha. Desliga em Ajustes › Estatísticas do projeto.
- **Aviso de pernoite no Discord**: só quando você marca (ou liga o automático), para a webhook que estiver em Ajustes ›
  Discord. A webhook padrão do projeto não fica no código: o workflow de Release a grava no instalador a partir do
  segredo `LINEOPS_DISCORD_WEBHOOK` do repositório. Rodando pelo código, não há padrão; cadastre a sua (ou use a
  variável de ambiente `LINEOPS_DISCORD_WEBHOOK`).
- **Documentos da Biblioteca** (MEL, CDL, QRH) ficam só no banco da sua instalação, como texto. Nada disso vem com a
  LineOps nem vai para o repositório.
- Consultas externas: RPL do CGNA, METAR/TAF (aviationweather.gov), IVAO (whazzup público), SimBrief (o seu plano),
  OpenStreetMap/OSRM (hotel e rota). Só leitura, a partir do seu PC.

## Backup e restauração

O backup leva a LineOps inteira: o piloto e o celular, todas as carreiras (escala, voos, diário, **mensagens da equipe**,
pernoites, pedidos de troca, acompanhamento da IVAO em andamento), o motor de escalas (escalas geradas e o histórico do RPL),
a cópia do AeroScale de cada carreira, os logos e o RPL baixado. Os bancos entram por foto consistente, mesmo com o servidor
rodando, com conferência de integridade. Os logs e a própria pasta de backups ficam de fora.

* **Sozinho:** um por dia enquanto a LineOps está aberta (guarda os 30 mais novos) e **um sempre que uma versão nova abre os
  seus dados**, antes de mexer em qualquer coisa (`…-antes-da-2.1.0.zip`, guarda os 10 mais novos, numa fila separada).
* **Na mão:** Ajustes → Backups → Criar backup agora; `backup.bat`; ou `python -m lineops backup`.
* **Restaurar pela tela:** Ajustes → Backups → Restaurar. A restauração fica agendada e é aplicada na próxima vez que a
  LineOps abrir, antes de qualquer banco ser aberto. Dá para enviar um .zip de outro PC pelo botão "Enviar um backup".
* **Restaurar pelo .bat:** `restaurar.bat` (arraste o .zip em cima, ou dois cliques para o mais recente), com a LineOps fechada.
* Antes de restaurar, o estado atual vira um backup `…-antes-de-restaurar.zip`: dá para voltar atrás.

**Atualizar no meio de um voo acompanhado pela IVAO:** pode. O acompanhamento fica gravado no banco e continua de onde parou
quando a LineOps abre de novo. O que acontecer enquanto ela estiver fechada não é lido: a trilha fica com um buraco, e a
LineOps registra a lacuna ("25 min sem leitura da IVAO…"). Se uma fase (decolagem, pouso) cair dentro da lacuna, o horário
dela sai do primeiro sinal depois, e a mensagem avisa quanto pode ter atrasado. Por isso, atualize em cruzeiro, longe da
decolagem e do pouso.

## Testes
`pip install -r requirements-dev.txt` e `python -m pytest tests -q` (importador, máquina de fases, serviço, HTTP, cliente ↔ servidor, X-Plane UDP, SimConnect com DLL simulada e salários).

## Limitações conhecidas
- Conectores de simulador **não foram testados em simuladores reais** (só com simuladores falsos). O primeiro voo real pode exigir ajustes (ex.: datarefs, offsets do SimConnect).
- O **centro de gravidade** ainda não é calculado: depende do peso e balanceamento da aeronave que você voa (as estações e
  seus braços estão no `flight_model.cfg` do PMDG e do LevelUp). O LOADSHEET sai com pesos e margens e diz isso na cara.
- **NOTAM** não entra: não há fonte pública confiável e gratuita.
- Os nomes dos campos do JSON do SimBrief não têm documentação oficial campo a campo; o leitor é tolerante e lista o que
  não veio, em vez de quebrar ou inventar.
- **Salários** são estimativa de simulação: GOL, LATAM e Azul têm acordos coletivos próprios acima da CCT (a LATAM paga
  por quilômetro, por exemplo). Para usar os seus valores, edite o contrato. A CCT não traz percentual de adicional noturno
  nem de periculosidade (ficam em 0 até você informar), e o AeroScale dá o dia da reserva, não o horário: cada dia de
  reserva não acionada vale 6 h (teto da cl. 3.3.12), ajustável. Feriados estaduais e municipais da base entram se você
  cadastrá-los no contrato.
- **IVAO**: o acompanhamento depende da internet do servidor e do whazzup público; não foi testado contra a rede ao vivo
  (só com leituras simuladas no formato da API v2). O voo é achado pelo VID, então conecte com o mesmo VID salvo em Ajustes.
- **Rede**: o histórico só existe a partir do dia em que o servidor ficou ligado com o VID salvo (o whazzup não tem
  passado). Um piloto conectado com outra companhia no callsign não aparece. Com o servidor desligado, não há amostras.
- Sem HTTPS nativo (use proxy reverso se precisar). Um único usuário.
