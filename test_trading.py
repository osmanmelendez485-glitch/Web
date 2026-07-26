from email.message import EmailMessage
import os
import smtplib
from dotenv import load_dotenv
import numpy as np
import pandas as pd
import yfinance as yf

# 1. Cargar las variables del archivo .env
load_dotenv()
import os
from dotenv import load_dotenv

# 1. Especificar el nombre exacto de tu archivo .env
load_dotenv('TW.env')

# 2. Lectura de las variables configuradas en TW.env
SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com').strip()
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))

raw_user = os.getenv('SMTP_USER', '')
SMTP_USER = raw_user.strip().strip('"').strip("'") if raw_user else None

raw_pass = os.getenv('SMTP_PASSWORD', '')
SMTP_PASSWORD = (
    raw_pass.strip().replace(' ', '').strip('"').strip("'")
    if raw_pass
    else None
)

raw_dest = os.getenv('EMAIL_DESTINO_DEFAULT', '')
EMAIL_DESTINO_DEFAULT = (
    raw_dest.strip().strip('"').strip("'") if raw_dest else SMTP_USER
)

DICCIONARIO_NOMBRES = {
    'GC=F': 'Oro (Refugio)',
    'SI=F': 'Plata (Refugio)',
    'HG=F': 'Cobre',
    'CL=F': 'Petroleo WTI',
    'ES=F': 'S&P 500',
    'BTC-USD': 'BITCOIN',
}


# --- FUNCIÓN DE ENVÍO ADAPTADA A TU PUERTO (587 / STARTTLS) ---
def enviar_email_smtp(asunto, cuerpo, destino=None):
  if not SMTP_USER or not SMTP_PASSWORD:
    print('❌ Error: Credenciales SMTP no encontradas en el .env')
    return False

  destinatario = (
      destino.strip()
      if destino and destino.strip()
      else EMAIL_DESTINO_DEFAULT
  )
  if not destinatario:
    print('❌ Error: No se definió EMAIL_DESTINO_DEFAULT.')
    return False

  msg = EmailMessage()
  msg.set_content(cuerpo)
  msg['Subject'] = asunto
  msg['From'] = SMTP_USER
  msg['To'] = destinatario

  try:
    if SMTP_PORT == 465:
      with smtplib.SMTP_SSL(
          SMTP_SERVER, SMTP_PORT, timeout=25
      ) as server:
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    else:
      # Manejo para puerto 587 (TLS)
      with smtplib.SMTP(
          SMTP_SERVER, SMTP_PORT, timeout=25
      ) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)

    print(f'✅ Email enviado exitosamente a {destinatario}')
    return True
  except Exception as e:
    print(f'❌ Error al enviar email: {e}')
    return False


def enviar_alerta_trading_email(orden, destino=None, puntos_max=6):
  asunto = f"🚀 Alerta Trading: {orden['nombre']} ({orden['accion']})"
  cuerpo = (
      f"🚀 OPORTUNIDAD TRADING\n\n"
      f"Instrumento: {orden['nombre']} ({orden['ticker']})\n"
      f"Acción: {orden['accion']}\n"
      f"Precio Actual: {orden['precio']}\n"
      f"ADX: {orden['adx_valor']} 🔥\n"
      f"Puntuación: {orden['puntos']}/{puntos_max}\n"
  )
  return enviar_email_smtp(asunto, cuerpo, destino)


# --- INDICADORES TÉCNICOS ---
def calcular_rsi(series, period=14):
  delta = series.diff()
  gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
  loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
  rs = gain / loss
  return 100 - (100 / (1 + rs))


def calcular_adx(df, period=14):
  high, low, close = df['High'], df['Low'], df['Close']
  tr1 = high - low
  tr2 = (high - close.shift(1)).abs()
  tr3 = (low - close.shift(1)).abs()
  tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
  atr = tr.rolling(window=period).mean()

  up_move = high - high.shift(1)
  down_move = low.shift(1) - low

  plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
  minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

  plus_di = (
      100
      * (pd.Series(plus_dm, index=df.index).rolling(window=period).mean() / atr)
  )
  minus_di = (
      100
      * (
          pd.Series(minus_dm, index=df.index).rolling(window=period).mean()
          / atr
      )
  )

  dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
  return dx.rolling(window=period).mean()


# --- ESCANEO ---
def escanear_e_informar(
    ticker, intervalo='1h', periodo='5d', puntos_min=5, email_dest=None
):
  nombre = DICCIONARIO_NOMBRES.get(ticker, ticker)

  df = yf.download(
      tickers=ticker,
      period=periodo,
      interval=intervalo,
      progress=False,
      auto_adjust=False,
      timeout=15,
  )

  if df is None or df.empty:
    return

  if isinstance(df.columns, pd.MultiIndex):
    df.columns = df.columns.get_level_values(0)

  if 'Close' not in df.columns and 'Adj Close' in df.columns:
    df['Close'] = df['Adj Close']

  df = df.ffill().dropna()

  if len(df) < 30:
    return

  precio_actual = float(df['Close'].iloc[-1])

  # ADX
  adx_serie = calcular_adx(df, period=14)
  if adx_serie is None or adx_serie.empty or pd.isna(adx_serie.iloc[-1]):
    return
  valor_adx = round(float(adx_serie.iloc[-1]), 2)

  if valor_adx < 25:
    print(f'ℹ️ {nombre}: ADX ({valor_adx}) < 25. Omitido.')
    return

  # Puntuación
  puntos_long, puntos_short = 3, 3

  sma50_serie = df['Close'].rolling(window=50).mean()
  if (
      sma50_serie is not None
      and not sma50_serie.empty
      and pd.notna(sma50_serie.iloc[-1])
  ):
    sma50_val = float(sma50_serie.iloc[-1])
    if precio_actual > sma50_val:
      puntos_long += 2
    else:
      puntos_short += 2

  rsi_serie = calcular_rsi(df['Close'], period=14)
  if (
      rsi_serie is not None
      and not rsi_serie.empty
      and pd.notna(rsi_serie.iloc[-1])
  ):
    rsi_val = float(rsi_serie.iloc[-1])
    if 40 < rsi_val < 60:
      puntos_long += 2
      puntos_short += 2

  puntos_finales = min(max(puntos_long, puntos_short), 6)
  accion = 'COMPRA/LONG' if puntos_long >= puntos_short else 'VENTA/SHORT'

  print(f'=== 📊 {nombre} ({ticker}) ===')
  print(f'  • Precio Actual  : {round(precio_actual, 4)}')
  print(f'  • ADX (14)       : {valor_adx}')
  print(f'  • Puntuación     : {puntos_finales}/6')

  if puntos_finales >= puntos_min:
    orden = {
        'ticker': ticker,
        'nombre': nombre,
        'precio': round(precio_actual, 4),
        'puntos': puntos_finales,
        'adx_valor': valor_adx,
        'accion': accion,
    }
    print('  📧 Enviando correo de prueba...')
    enviar_alerta_trading_email(
        orden, destino=email_dest, puntos_max=puntos_min
    )
  else:
    print(f'  ℹ️ Puntuación insuficiente ({puntos_finales} < {puntos_min})')

  print('-' * 40)


if __name__ == '__main__':
  # Asegúrate de tener python-dotenv instalado (pip install python-dotenv)
  tickers_test = ['GC=F', 'CL=F', 'BTC-USD']
  for t in tickers_test:
    escanear_e_informar(
        t,
        intervalo='1h',
        periodo='5d',
        puntos_min=5,
        email_dest=EMAIL_DESTINO_DEFAULT,
    )