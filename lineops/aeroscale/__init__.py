"""Motor do AeroScale 2.2.4 dentro da LineOps.

O AeroScale é projeto do mesmo autor, e a partir da LineOps ele vive aqui dentro: o leitor de RPL do CGNA, a atualização
semanal do RPL, o gerador de escalas (escala do tripulante, calibrada com escalas reais de comandante), as regras de
tripulação (Lei 13.475, RBAC 117), o rodízio observado no ADS-B, reservas, internacionais e a importação de escala real.

Os arquivos são cópias LITERAIS do AeroScale 2.2.4. A única mudança é em db.py: o import do Flask virou opcional, porque o
motor roda sem o aplicativo web do AeroScale. Para atualizar o motor, copie os arquivos novos do AeroScale por cima desta
pasta (e os RPL em rpl/). Fica de fora o que é do aplicativo dele (telas, API Flask, ACARS próprio, Google Calendar):
a LineOps tem os seus.
"""
__version__ = "2.2.4"          # versão do AeroScale de onde o motor veio (o rpl_update usa no User-Agent)
SOURCE_VERSION = __version__
