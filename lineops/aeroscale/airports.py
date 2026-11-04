"""Tabela de aeroportos e utilitários de fuso horário.

A tabela embutida cobre os aeroportos encontrados nos RPL de AZU, GLO e TAM
(validade 22/10/2026). Aeroportos ausentes podem ser cadastrados pela tela de
Configurações (ficam na tabela `airports` do banco e têm prioridade).

Horários do RPL (EOBT) estão em UTC. Para exibir horário local usamos IANA
(zoneinfo). No Windows o pacote `tzdata` fornece a base de fusos; se ele não
estiver instalado, caímos para os deslocamentos fixos do Brasil (sem horário de
verão desde 2019).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:  # pragma: no cover - depende do sistema
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# ICAO: (IATA, nome, cidade, UF, fuso IANA)
BUILTIN_AIRPORTS: dict[str, tuple[str, str, str, str, str]] = {
    "SBAC": ("ARX", "Dragão do Mar", "Aracati", "CE", "America/Fortaleza"),
    "SBAE": ("JTC", "Moussa Nakhl Tobias", "Bauru/Arealva", "SP", "America/Sao_Paulo"),
    "SBAR": ("AJU", "Santa Maria", "Aracaju", "SE", "America/Maceio"),
    "SBAT": ("AFL", "Piloto Osvaldo Marques Dias", "Alta Floresta", "MT", "America/Cuiaba"),
    "SBAU": ("ARU", "Dario Guarita", "Araçatuba", "SP", "America/Sao_Paulo"),
    "SBBE": ("BEL", "Val-de-Cans", "Belém", "PA", "America/Belem"),
    "SBBR": ("BSB", "Presidente Juscelino Kubitschek", "Brasília", "DF", "America/Sao_Paulo"),
    "SBBV": ("BVB", "Atlas Brasil Cantanhede", "Boa Vista", "RR", "America/Boa_Vista"),
    "SBCA": ("CAC", "Adalberto Mendes da Silva", "Cascavel", "PR", "America/Sao_Paulo"),
    "SBCF": ("CNF", "Tancredo Neves (Confins)", "Belo Horizonte", "MG", "America/Sao_Paulo"),
    "SBCG": ("CGR", "Campo Grande", "Campo Grande", "MS", "America/Campo_Grande"),
    "SBCH": ("XAP", "Serafin Enoss Bertaso", "Chapecó", "SC", "America/Sao_Paulo"),
    "SBCJ": ("CKS", "Carajás", "Parauapebas", "PA", "America/Belem"),
    "SBCN": ("CLV", "Nelson Ribeiro Guimarães", "Caldas Novas", "GO", "America/Sao_Paulo"),
    "SBCR": ("CMG", "Corumbá", "Corumbá", "MS", "America/Campo_Grande"),
    "SBCT": ("CWB", "Afonso Pena", "Curitiba", "PR", "America/Sao_Paulo"),
    "SBCX": ("CXJ", "Hugo Cantergiani", "Caxias do Sul", "RS", "America/Sao_Paulo"),
    "SBCY": ("CGB", "Marechal Rondon", "Cuiabá/Várzea Grande", "MT", "America/Cuiaba"),
    "SBCZ": ("CZS", "Cruzeiro do Sul", "Cruzeiro do Sul", "AC", "America/Rio_Branco"),
    "SBDB": ("BYO", "Bonito", "Bonito", "MS", "America/Campo_Grande"),
    "SBDN": ("PPB", "Presidente Prudente", "Presidente Prudente", "SP", "America/Sao_Paulo"),
    "SBDO": ("DOU", "Dourados", "Dourados", "MS", "America/Campo_Grande"),
    "SBEG": ("MAO", "Eduardo Gomes", "Manaus", "AM", "America/Manaus"),
    "SBFI": ("IGU", "Cataratas", "Foz do Iguaçu", "PR", "America/Sao_Paulo"),
    "SBFL": ("FLN", "Hercílio Luz", "Florianópolis", "SC", "America/Sao_Paulo"),
    "SBFN": ("FEN", "Fernando de Noronha", "Fernando de Noronha", "PE", "America/Noronha"),
    "SBFZ": ("FOR", "Pinto Martins", "Fortaleza", "CE", "America/Fortaleza"),
    "SBGL": ("GIG", "Galeão – Tom Jobim", "Rio de Janeiro", "RJ", "America/Sao_Paulo"),
    "SBGO": ("GYN", "Santa Genoveva", "Goiânia", "GO", "America/Sao_Paulo"),
    "SBGR": ("GRU", "Guarulhos – Gov. André Franco Montoro", "São Paulo", "SP", "America/Sao_Paulo"),
    "SBGV": ("GVR", "Coronel Altino Machado", "Governador Valadares", "MG", "America/Sao_Paulo"),
    "SBHT": ("ATM", "Altamira", "Altamira", "PA", "America/Belem"),
    "SBIH": ("ITB", "Itaituba", "Itaituba", "PA", "America/Santarem"),
    "SBIL": ("IOS", "Jorge Amado", "Ilhéus", "BA", "America/Bahia"),
    "SBIP": ("IPN", "Usiminas", "Ipatinga", "MG", "America/Sao_Paulo"),
    "SBIZ": ("IMP", "Prefeito Renato Moreira", "Imperatriz", "MA", "America/Fortaleza"),
    "SBJA": ("JJG", "Humberto Ghizzo Bortoluzzi", "Jaguaruna", "SC", "America/Sao_Paulo"),
    "SBJE": ("JJD", "Comandante Ariston Pessoa", "Jijoca de Jericoacoara", "CE", "America/Fortaleza"),
    "SBJI": ("JPR", "José Coleto", "Ji-Paraná", "RO", "America/Porto_Velho"),
    "SBJP": ("JPA", "Presidente Castro Pinto", "João Pessoa", "PB", "America/Fortaleza"),
    "SBJU": ("JDO", "Orlando Bezerra de Menezes", "Juazeiro do Norte", "CE", "America/Fortaleza"),
    "SBJV": ("JOI", "Lauro Carneiro de Loyola", "Joinville", "SC", "America/Sao_Paulo"),
    "SBKG": ("CPV", "Presidente João Suassuna", "Campina Grande", "PB", "America/Fortaleza"),
    "SBKP": ("VCP", "Viracopos", "Campinas", "SP", "America/Sao_Paulo"),
    "SBLE": ("LEC", "Horácio de Mattos", "Lençóis", "BA", "America/Bahia"),
    "SBLO": ("LDB", "Governador José Richa", "Londrina", "PR", "America/Sao_Paulo"),
    "SBMA": ("MAB", "João Corrêa da Rocha", "Marabá", "PA", "America/Belem"),
    "SBMG": ("MGF", "Sílvio Name Júnior", "Maringá", "PR", "America/Sao_Paulo"),
    "SBMK": ("MOC", "Mário Ribeiro", "Montes Claros", "MG", "America/Sao_Paulo"),
    "SBML": ("MII", "Frank Miloye Milenkovich", "Marília", "SP", "America/Sao_Paulo"),
    "SBMO": ("MCZ", "Zumbi dos Palmares", "Maceió", "AL", "America/Maceio"),
    "SBMQ": ("MCP", "Alberto Alcolumbre", "Macapá", "AP", "America/Belem"),
    "SBMS": ("MVF", "Dix-Sept Rosado", "Mossoró", "RN", "America/Fortaleza"),
    "SBNF": ("NVT", "Ministro Victor Konder", "Navegantes", "SC", "America/Sao_Paulo"),
    "SBNM": ("GEL", "Sepé Tiaraju", "Santo Ângelo", "RS", "America/Sao_Paulo"),
    "SBOI": ("OYK", "Oiapoque", "Oiapoque", "AP", "America/Belem"),
    "SBPA": ("POA", "Salgado Filho", "Porto Alegre", "RS", "America/Sao_Paulo"),
    "SBPB": ("PHB", "Prefeito Dr. João Silva Filho", "Parnaíba", "PI", "America/Fortaleza"),
    "SBPF": ("PFB", "Lauro Kurtz", "Passo Fundo", "RS", "America/Sao_Paulo"),
    "SBPJ": ("PMW", "Brigadeiro Lysias Rodrigues", "Palmas", "TO", "America/Araguaina"),
    "SBPK": ("PET", "João Simões Lopes Neto", "Pelotas", "RS", "America/Sao_Paulo"),
    "SBPL": ("PNZ", "Senador Nilo Coelho", "Petrolina", "PE", "America/Recife"),
    "SBPO": ("PTO", "Juvenal Loureiro Cardoso", "Pato Branco", "PR", "America/Sao_Paulo"),
    "SBPP": ("PMG", "Ponta Porã", "Ponta Porã", "MS", "America/Campo_Grande"),
    "SBPS": ("BPS", "Porto Seguro", "Porto Seguro", "BA", "America/Bahia"),
    "SBPV": ("PVH", "Governador Jorge Teixeira", "Porto Velho", "RO", "America/Porto_Velho"),
    "SBRB": ("RBR", "Plácido de Castro", "Rio Branco", "AC", "America/Rio_Branco"),
    "SBRD": ("ROO", "Maestro Marinho Franco", "Rondonópolis", "MT", "America/Cuiaba"),
    "SBRF": ("REC", "Guararapes – Gilberto Freyre", "Recife", "PE", "America/Recife"),
    "SBRJ": ("SDU", "Santos Dumont", "Rio de Janeiro", "RJ", "America/Sao_Paulo"),
    "SBRP": ("RAO", "Leite Lopes", "Ribeirão Preto", "SP", "America/Sao_Paulo"),
    "SBSG": ("NAT", "Governador Aluízio Alves", "São Gonçalo do Amarante (Natal)", "RN", "America/Fortaleza"),
    "SBSI": ("OPS", "Presidente João Figueiredo", "Sinop", "MT", "America/Cuiaba"),
    "SBSJ": ("SJK", "Professor Urbano Ernesto Stumpf", "São José dos Campos", "SP", "America/Sao_Paulo"),
    "SBSL": ("SLZ", "Marechal Cunha Machado", "São Luís", "MA", "America/Fortaleza"),
    "SBSM": ("RIA", "Santa Maria", "Santa Maria", "RS", "America/Sao_Paulo"),
    "SBSN": ("STM", "Maestro Wilson Fonseca", "Santarém", "PA", "America/Santarem"),
    "SBSO": ("SMT", "Adolino Bedin", "Sorriso", "MT", "America/Cuiaba"),
    "SBSP": ("CGH", "Congonhas", "São Paulo", "SP", "America/Sao_Paulo"),
    "SBSR": ("SJP", "Professor Eribelto Manoel Reino", "São José do Rio Preto", "SP", "America/Sao_Paulo"),
    "SBSV": ("SSA", "Deputado Luís Eduardo Magalhães", "Salvador", "BA", "America/Bahia"),
    "SBTB": ("TMT", "Trombetas", "Oriximiná", "PA", "America/Santarem"),
    "SBTC": ("UNA", "Comandatuba", "Una", "BA", "America/Bahia"),
    "SBTE": ("THE", "Senador Petrônio Portella", "Teresina", "PI", "America/Fortaleza"),
    "SBTF": ("TFF", "Tefé", "Tefé", "AM", "America/Manaus"),
    "SBTT": ("TBT", "Tabatinga", "Tabatinga", "AM", "America/Manaus"),
    "SBUA": ("SJL", "São Gabriel da Cachoeira", "São Gabriel da Cachoeira", "AM", "America/Manaus"),
    "SBUG": ("URG", "Rubem Berta", "Uruguaiana", "RS", "America/Sao_Paulo"),
    "SBUL": ("UDI", "Ten. Cel. Av. César Bombonato", "Uberlândia", "MG", "America/Sao_Paulo"),
    "SBUR": ("UBA", "Mário de Almeida Franco", "Uberaba", "MG", "America/Sao_Paulo"),
    "SBUY": ("RPU", "Urucu", "Coari (Urucu)", "AM", "America/Manaus"),
    "SBVC": ("VDC", "Glauber Rocha", "Vitória da Conquista", "BA", "America/Bahia"),
    "SBVH": ("BVH", "Brigadeiro Camarão", "Vilhena", "RO", "America/Porto_Velho"),
    "SBVT": ("VIX", "Eurico de Aguiar Salles", "Vitória", "ES", "America/Sao_Paulo"),
    "SBZM": ("IZA", "Presidente Itamar Franco", "Juiz de Fora (Goianá)", "MG", "America/Sao_Paulo"),
    "SNBR": ("BRA", "Barreiras", "Barreiras", "BA", "America/Bahia"),
    "SNGI": ("GNM", "Guanambi", "Guanambi", "BA", "America/Bahia"),
    "SSKW": ("OAL", "Capital do Café", "Cacoal", "RO", "America/Porto_Velho"),
    "SWGN": ("AUX", "Araguaína", "Araguaína", "TO", "America/Araguaina"),
    "SWPI": ("PIN", "Júlio Belém", "Parintins", "AM", "America/Manaus"),
    # SNCP (RPL da GOL): identificado pela base OurAirports em 28/10/2026.
    "SNCP": ("EEA", "Planalto Serrano", "Correia Pinto (Lages)", "SC", "America/Sao_Paulo"),
}

# Deslocamentos fixos (minutos) usados apenas se zoneinfo/tzdata falhar.
FIXED_OFFSETS = {
    "UTC": 0,
    "America/Noronha": -120,
    "America/Sao_Paulo": -180, "America/Fortaleza": -180, "America/Recife": -180,
    "America/Bahia": -180, "America/Maceio": -180, "America/Belem": -180,
    "America/Santarem": -180, "America/Araguaina": -180,
    "America/Manaus": -240, "America/Cuiaba": -240, "America/Campo_Grande": -240,
    "America/Porto_Velho": -240, "America/Boa_Vista": -240,
    "America/Rio_Branco": -300, "America/Eirunepe": -300,
}

_tz_cache: dict[str, object] = {}


def get_tz(name: str | None):
    """Retorna um tzinfo para o nome IANA; UTC se vazio/desconhecido."""
    if not name:
        return timezone.utc
    if name in _tz_cache:
        return _tz_cache[name]
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(name)
        except Exception:
            tz = None
    if tz is None:
        off = FIXED_OFFSETS.get(name)
        tz = timezone(timedelta(minutes=off), name) if off is not None else None
    if tz is None:
        return None
    _tz_cache[name] = tz
    return tz


def valid_tz(name: str) -> bool:
    return get_tz(name) is not None


def to_local(dt_utc: datetime, tzname: str | None) -> datetime:
    tz = get_tz(tzname) or timezone.utc
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(tz)


def offset_label(dt_local: datetime) -> str:
    off = dt_local.utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    mins = abs(mins)
    h, m = divmod(mins, 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")
