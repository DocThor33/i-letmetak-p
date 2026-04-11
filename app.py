import streamlit as st
import google.generativeai as genai
import pandas as pd # type: ignore
from PIL import Image
import io
import sqlite3
import os
import base64
import requests
from reportlab.platypus import SimpleDocTemplate, Paragraph # type: ignore
from reportlab.lib.styles import getSampleStyleSheet # type: ignore
from openpyxl.styles import Font, PatternFill, Alignment # type: ignore
from datetime import datetime
import calendar
import traceback

# Her deployda surumu takip etmek icin gorunen build etiketi.
APP_BUILD = "2026-04-11-2"

# --- 1. YENİ API KEY VE DİNAMİK MODEL SEÇİCİ ---
# REST yolundaki latin-1 header sorunlarina karsi once gRPC dene, olmazsa varsayilana don.
API_KEY = st.secrets.get("GOOGLE_API_KEY", "")

try:
    genai.configure(api_key=API_KEY, transport="grpc")
    GENAI_TRANSPORT = "grpc"
except Exception:
    genai.configure(api_key=API_KEY)
    GENAI_TRANSPORT = "default"
@st.cache_resource
def model_tespit_et():
    try:
        # list_models bazen metadata hatası verebiliyor; sabit fallback zinciri daha güvenli.
        oncelik = [
            'gemini-1.5-flash',
            'gemini-1.5-pro',
            'gemini-1.0-pro'
        ]

        for m_adi in oncelik:
            try:
                return genai.GenerativeModel(m_adi), m_adi
            except Exception:
                continue

        return None, "Uygun model bulunamadi. API anahtari/model yetkilerini kontrol et."

    except Exception as e:
        return None, f"Hata: {e}"

# Modeli ve ismini belirle
model, aktif_surum = model_tespit_et()

def gemini_generate_via_rest(prompt, image_bytes, mime_type, model_name):
    """SDK'nin header encoding hatalarina karsi dogrudan REST cagrisi."""
    base_model = (model_name or "gemini-1.5-flash").replace("models/", "")

    def _list_models(api_version):
        endpoint = f"https://generativelanguage.googleapis.com/{api_version}/models?key={API_KEY}"
        resp = requests.get(endpoint, timeout=30)
        if resp.status_code != 200:
            return []

        items = resp.json().get("models", [])
        available = []
        for item in items:
            name = item.get("name", "").replace("models/", "")
            methods = item.get("supportedGenerationMethods", [])
            if name and "generateContent" in methods:
                available.append(name)
        return available

    def _rank_models(models, preferred):
        # Tercih sirasi: birebir model > flash > pro > diger
        if not models:
            return []

        def score(m):
            if m == preferred:
                return 0
            if "flash" in m:
                return 1
            if "pro" in m:
                return 2
            return 3

        return sorted(models, key=lambda m: (score(m), m))

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64.b64encode(image_bytes).decode("ascii")
                        }
                    }
                ]
            }
        ]
    }

    last_404 = None
    for api_version in ["v1beta", "v1"]:
        discovered_models = _rank_models(_list_models(api_version), base_model)
        model_candidates = discovered_models if discovered_models else [base_model]

        for candidate in model_candidates:
            endpoint = f"https://generativelanguage.googleapis.com/{api_version}/models/{candidate}:generateContent?key={API_KEY}"
            resp = requests.post(endpoint, headers={"Content-Type": "application/json"}, json=payload, timeout=60)

            if resp.status_code == 404:
                last_404 = f"{api_version}/{candidate}: {resp.text[:250]}"
                continue

            if resp.status_code != 200:
                raise Exception(f"Gemini REST HTTP {resp.status_code}: {resp.text[:500]}")

            body = resp.json()
            candidates = body.get("candidates", [])
            if not candidates:
                raise Exception(f"Gemini REST bos aday dondu: {body}")

            parts = candidates[0].get("content", {}).get("parts", [])
            text_chunks = [p.get("text", "") for p in parts if "text" in p]
            result = "\n".join([t for t in text_chunks if t]).strip()
            if not result:
                raise Exception(f"Gemini REST metin dondurmedi: {body}")
            return result

    raise Exception(f"Gemini REST uygun model bulunamadi. Son 404: {last_404}")

# --- 2. VERİTABANI İŞLEMLERİ ---
def ay_bul(tarih_str):
    """Tarih stringinden ay ve yıl çıkar"""
    try:
        # Farklı tarih formatlarını dene
        for fmt in ['%d.%m.%Y', '%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y']:
            try:
                dt = datetime.strptime(tarih_str.strip(), fmt)
                return dt.year, dt.month
            except ValueError:
                continue
        # Eğer hiçbiri çalışmazsa, şu anki tarihi kullan
        now = datetime.now()
        return now.year, now.month
    except:
        now = datetime.now()
        return now.year, now.month

def veritabani_adi_getir(yil=None, ay=None):
    """Ay bazlı veritabanı dosya adı oluştur"""
    if yil is None or ay is None:
        now = datetime.now()
        yil, ay = now.year, now.month
    return f"giderler_{yil}_{ay:02d}.db"

def veritabani_kur(yil=None, ay=None):
    db_adi = veritabani_adi_getir(yil, ay)
    conn = sqlite3.connect(db_adi)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS harcamalar 
                 (firma TEXT, tarih TEXT, kategori TEXT, kalem TEXT, miktar TEXT, birim_fiyat TEXT, toplam_fiyat TEXT)''')
    conn.commit()
    conn.close()

def veriye_kaydet(df, yil=None, ay=None):
    if df.empty:
        return
    
    # Eğer yıl/ay belirtilmemişse, ilk satırdaki tarihten çıkar
    if yil is None or ay is None:
        if 'tarih' in df.columns and not df['tarih'].empty:
            ilk_tarih = str(df['tarih'].iloc[0])
            yil, ay = ay_bul(ilk_tarih)
    
    db_adi = veritabani_adi_getir(yil, ay)
    veritabani_kur(yil, ay)  # Veritabanını oluştur
    
    conn = sqlite3.connect(db_adi)
    
    # BU SATIRI EKLE: Sütun isimleri veritabanıyla %100 aynı olsun diye zorluyoruz
    df.columns=["firma", "tarih", "kategori", "kalem", "miktar", "birim_fiyat", "toplam_fiyat"]
    
    df.to_sql('harcamalar', conn, if_exists='append', index=False)
    conn.commit()
    conn.close()

def verileri_getir(yil=None, ay=None):
    """Belirtilen ayın verilerini getir"""
    db_adi = veritabani_adi_getir(yil, ay)
    
    if not os.path.exists(db_adi):
        return pd.DataFrame()
    
    conn = sqlite3.connect(db_adi)
    df = pd.read_sql_query("SELECT * FROM harcamalar", conn)
    conn.close()
    return df

def tum_aylari_getir():
    """Mevcut tüm ay veritabanı dosyalarını listele"""
    import glob
    db_files = glob.glob("giderler_*.db")
    aylar = []
    
    for db_file in db_files:
        # giderler_2026_04.db formatından yıl ve ay çıkar
        try:
            parts = db_file.replace('giderler_', '').replace('.db', '').split('_')
            if len(parts) == 2:
                yil, ay = int(parts[0]), int(parts[1])
                aylar.append((yil, ay))
        except:
            continue
    
    # Yıla ve aya göre sırala (en yeni en üstte)
    aylar.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return aylar

def excel_dosya_olustur_ve_kaydet(yil=None, ay=None):
    """Excel dosyasını oluştur, paylaş ve klasöre kaydet"""
    import openpyxl
    from openpyxl.utils import get_column_letter
    
    df = verileri_getir(yil, ay)
    
    if df.empty:
        return None, "Kaydedilecek veri yok!"
    
    ay_adi = calendar.month_name[ay] if ay else "Tüm"
    yil_str = str(yil) if yil else "Tüm"
    excel_dosyasi = f"Harcamalar_{yil_str}_{ay:02d}_{ay_adi}.xlsx"
    
    with pd.ExcelWriter(excel_dosyasi, engine='openpyxl') as writer:
        # 1. Ana Veri Sayfası
        df.to_excel(writer, sheet_name='Faturalar', index=False)
        
        # 2. Kategori Özeti
        try:
            temp_df = df.copy()
            temp_df['toplam_fiyat_sayi'] = (
                temp_df['toplam_fiyat']
                .str.replace('TL', '', regex=False)
                .str.strip()
                .str.replace('.', '', regex=False)
                .str.replace(',', '.', regex=False)
            )
            temp_df['toplam_fiyat_sayi'] = pd.to_numeric(temp_df['toplam_fiyat_sayi'], errors='coerce')
            
            kategori_ozet = temp_df.dropna(subset=['kategori']).groupby('kategori')['toplam_fiyat_sayi'].sum().reset_index()
            kategori_ozet.columns = ['Kategori', 'Toplam (TL)']
            kategori_ozet = kategori_ozet.sort_values('Toplam (TL)', ascending=False)
            kategori_ozet.to_excel(writer, sheet_name='Kategori Özeti', index=False)
            
            # 3. Firma Özeti
            firma_ozet = temp_df.dropna(subset=['firma']).groupby('firma')['toplam_fiyat_sayi'].sum().reset_index()
            firma_ozet.columns = ['Firma', 'Toplam (TL)']
            firma_ozet = firma_ozet.sort_values('Toplam (TL)', ascending=False)
            firma_ozet.to_excel(writer, sheet_name='Firma Özeti', index=False)
        except Exception as e:
            st.warning(f"Özet sayfaları oluşturulurken hata: {e}")
    
    # Dosyayı oku ve döndür
    with open(excel_dosyasi, 'rb') as f:
        output = io.BytesIO(f.read())
    
    output.seek(0)
    return output, f"✅ Excel dosyası kaydedildi: **{excel_dosyasi}**"

veritabani_kur()

# --- 3. ARAYÜZ ---
st.set_page_config(page_title="Sote Pilav Muhasebe", layout="wide")
st.sidebar.caption(f"Build: {APP_BUILD} | Transport: {GENAI_TRANSPORT}")

# Hangi sürümün çalıştığını burada gösteriyoruz
if "Hata" in aktif_surum:
    st.error(f"⚠️ {aktif_surum}")
else:
    st.success(f"🤖 Aktif Kullanılan AI Sürümü: **{aktif_surum}**")

# --- 4. YAN PANEL - SAYFA SEÇİCİ ---
st.sidebar.header("📱 Menü")
sayfa = st.sidebar.radio("Sayfayı seç:", ["📝 Fatura Yükle", "📊 İstatistikler", "📚 Tüm Veriler", "🧾 Komisyon Hesapla"])

# Ay seçici
mevcut_aylar = tum_aylari_getir()
mevcut_yillar = sorted({yil for yil, ay in mevcut_aylar} | {datetime.now().year}, reverse=True)

aylar = [calendar.month_name[i] for i in range(1, 13)]

secilen_yil = st.sidebar.selectbox("📅 Yıl Seç:", mevcut_yillar, index=0)
secilen_ay_adi = st.sidebar.selectbox("📅 Ay Seç:", aylar, index=datetime.now().month - 1)
secilen_ay_num = aylar.index(secilen_ay_adi) + 1

# Eğer seçili ay için veri yoksa kullanıcıya bilgi ver
if not mevcut_aylar:
    st.sidebar.info("Henüz kayıtlı veri yok.")

st.sidebar.divider()
st.sidebar.header("🗓️ Ay Sonu İşlemleri")
birikmis_veri = verileri_getir(secilen_yil, secilen_ay_num)

if not birikmis_veri.empty:
    st.sidebar.subheader("💰 Harcama Özeti")
    # Verileri temizleyip sayıya çeviriyoruz (TL yazılarını atıp toplamak için)
    try:
        temp_df = birikmis_veri.copy()
        # Toplam fiyat sütunundaki 'TL', boşluk, nokta ve virgülleri temizle
        temp_df['toplam_fiyat_sayi'] = (
            temp_df['toplam_fiyat']
            .str.replace('TL', '', regex=False)  # TL yazısını kaldır
            .str.strip()  # Başındaki/sonundaki boşlukları kaldır
            .str.replace('.', '', regex=False)  # Nokta (binler ayırıcısı) kaldır
            .str.replace(',', '.', regex=False)  # Virgülü noktaya çevir
        )
        temp_df['toplam_fiyat_sayi'] = pd.to_numeric(temp_df['toplam_fiyat_sayi'], errors='coerce')
        
        # 1. Genel Toplam
        toplam_tutar = temp_df['toplam_fiyat_sayi'].sum()
        fatura_sayisi = len(temp_df)
        st.sidebar.metric("💰 Toplam Harcama", f"{toplam_tutar:.2f} TL")
        st.sidebar.metric("📄 Fatura Sayısı", fatura_sayisi)
        
    except Exception as e:
        st.sidebar.warning(f"⚠️ Fiyat formatı sorunu: {e}")

# Excel İndirme Butonu
output, excel_msg = excel_dosya_olustur_ve_kaydet(secilen_yil, secilen_ay_num)

if output:
    col1, col2, col3 = st.sidebar.columns(3)
    with col1:
        ay_adi = calendar.month_name[secilen_ay_num]
        dosya_adi = f"Harcamalar_{secilen_yil}_{secilen_ay_num:02d}_{ay_adi}.xlsx"
        st.sidebar.download_button("📥 Excel Raporu Al", output.getvalue(), dosya_adi)
    
    with col2:
        if st.sidebar.button("💾 Kaydet"):
            st.sidebar.success(excel_msg)
    
    with col3:
        if st.sidebar.button("📄 PDF Indir"):
            st.sidebar.info("PDF özelliği yakında gelecek!")
else:
    col1, col2, col3 = st.sidebar.columns(3)
    with col2:
        if st.sidebar.button("📄 PDF Indir"):
            st.sidebar.info("PDF özelliği yakında gelecek!")

if st.sidebar.button("🗑️ Veriyi Sil"):
    ay_adi = calendar.month_name[secilen_ay_num]
    st.session_state.delete_confirm = f"{ay_adi} {secilen_yil} ayının TÜM verilerini silmek istediğinizden emin misiniz?"

# Silme onayı
if st.session_state.get('delete_confirm', False):
    st.sidebar.warning(f"⚠️ {st.session_state.delete_confirm}")
    st.sidebar.warning("Geri alınamaz!")
    col_evet, col_hayir = st.sidebar.columns(2)
    
    with col_evet:
        if st.sidebar.button("✅ Eminim, Sil", key="btn_sil_onayla"):
            # Seçili ayın veritabanı dosyasını sil
            db_adi = veritabani_adi_getir(secilen_yil, secilen_ay_num)
            if os.path.exists(db_adi):
                os.remove(db_adi)
            st.session_state.delete_confirm = False
            ay_adi = calendar.month_name[secilen_ay_num]
            st.sidebar.success(f"✨ {ay_adi} {secilen_yil} verileri silindi!")
            st.rerun()
    
    with col_hayir:
        if st.sidebar.button("❌ İptal", key="btn_sil_iptal"):
            st.session_state.delete_confirm = False
            st.rerun()

# --- 5. ANA EKRAN (ANALİZ VE KAYIT) ---
if sayfa == "📝 Fatura Yükle":
    st.title("📝 Fatura Yükle")
    uploaded_file = st.file_uploader("Fatura Fotoğrafı Yükle", type=["jpg", "jpeg", "png"])
    image = None
    image_bytes = None

    if uploaded_file:
        image_bytes = uploaded_file.getvalue()
        image = Image.open(io.BytesIO(image_bytes))
        col1, col2 = st.columns(2)
        
        with col1:
            st.image(image, width='stretch')
        
    if st.button("🚀 Faturayı Analiz Et"):
        if uploaded_file is None:
            st.warning("Lütfen önce bir fatura yükle.")
        elif model is None:
            st.error("AI modeli hazir degil. API anahtarini ve model erisimini kontrol edin.")
        else:
            with st.spinner('Yapay zeka analiz ediyor...'):
                ...
            prompt = """
This is an invoice image.

Rules:
- Use only semicolon (; ) as separator
- Do not add explanations
- Return rows only

Format:
Company;Date;Category;Item;Quantity;UnitPrice;Total
"""
            try:
                # Dosya adindaki ozel karakterlerden etkilenmemek icin gorseli ham byte olarak gonder.
                mime_type = uploaded_file.type or "image/jpeg"
                try:
                    response = model.generate_content([
                        prompt,
                        {"inline_data": {"mime_type": mime_type, "data": image_bytes}}
                    ])
                    raw_text = response.text.strip().replace("```csv", "").replace("```", "").strip()
                except Exception as sdk_err:
                    if "latin-1" in str(sdk_err).lower():
                        raw_text = gemini_generate_via_rest(prompt, image_bytes, mime_type, aktif_surum)
                        raw_text = raw_text.replace("```csv", "").replace("```", "").strip()
                    else:
                        raise

                data_lines = []
                for l in raw_text.split('\n'):
                    parts = l.split(';')
                    if len(parts) == 7:
                        data_lines.append(parts)
                
                # Sütun isimlerini biz el ile (sabit) veriyoruz:
                df_temp = pd.DataFrame(data_lines, columns=["firma", "tarih", "kategori", "kalem", "miktar", "birim_fiyat", "toplam_fiyat"])
                st.session_state['onay_bekleyen'] = df_temp
            except Exception as e:
                err_text = str(e)
                err_lower = err_text.lower()
                if "api_key_invalid" in err_lower or "api key not valid" in err_lower or "invalid api key" in err_lower:
                    st.error("API key gecersiz. Streamlit Secrets'taki GOOGLE_API_KEY degerini gercek ve aktif bir Gemini API key ile guncelle.")
                elif "api key was reported as leaked" in err_lower:
                    st.error("Google API key guvenlik nedeniyle devre disi kalmis. Streamlit Secrets'ta yeni API key tanimlayip yeniden deploy etmelisin.")
                elif "latin-1" in err_lower:
                    st.error("Unicode/language kaynakli header encoding hatasi olustu. Yeni API key ile tekrar dene ve dosya adini ASCII karakterlerle yukle (or: fatura_01.jpg).")
                else:
                    st.error(f"Analiz sırasında hata: {e}")
                with st.expander("Teknik hata detayi"):
                    st.code(traceback.format_exc())
    
    # Analiz sonucu düzenleme bölümü - sadece bu sayfada göster
    if 'onay_bekleyen' in st.session_state:
        st.subheader("📋 Analiz Sonucu (Düzenlemek için hücrelere tıklayabilirsin)")
        
        # Düzenlenebilir tablo
        duzenlenmis_df = st.data_editor(st.session_state['onay_bekleyen'], width='stretch', num_rows="dynamic")
        
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("💾 Onayla ve Hafızaya Kaydet", key="kaydet_ozel_1"):
                # Kaydederken AI'nın ilk halini değil, senin düzelttiğin (duzenlenmis_df) halini gönderiyoruz:
                veriye_kaydet(duzenlenmis_df)
                st.success("Başarıyla kaydedildi!")
                del st.session_state['onay_bekleyen']
                st.rerun()
        
        with col2:
            if st.button("🗑️ İptal Et", key="iptal_et"):
                del st.session_state['onay_bekleyen']
                st.info("Analiz sonucu iptal edildi.")
                st.rerun()

elif sayfa == "📊 İstatistikler":
    ay_adi = calendar.month_name[secilen_ay_num]
    st.title(f"📊 {ay_adi} {secilen_yil} İstatistikleri")
    
    if not birikmis_veri.empty:
        try:
            temp_df = birikmis_veri.copy()
            temp_df['toplam_fiyat_sayi'] = (
                temp_df['toplam_fiyat']
                .str.replace('TL', '', regex=False)
                .str.strip()
                .str.replace('.', '', regex=False)
                .str.replace(',', '.', regex=False)
            )
            temp_df['toplam_fiyat_sayi'] = pd.to_numeric(temp_df['toplam_fiyat_sayi'], errors='coerce')
            
            # Özet Metrikleri
            col1, col2, col3 = st.columns(3)
            with col1:
                toplam_tutar = temp_df['toplam_fiyat_sayi'].sum()
                st.metric("💰 Toplam Harcama", f"{toplam_tutar:.2f} TL")
            with col2:
                fatura_sayisi = len(temp_df)
                st.metric("📄 Fatura Sayısı", fatura_sayisi)
            with col3:
                ort_fatura = toplam_tutar / fatura_sayisi if fatura_sayisi > 0 else 0
                st.metric("📊 Ort. Fatura", f"{ort_fatura:.2f} TL")
            
            st.divider()
            
            # Grafikler
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader("📊 Kategori Harcama")
                try:
                    kategori_chart = temp_df.dropna(subset=['kategori']).groupby("kategori")["toplam_fiyat_sayi"].sum()
                    if not kategori_chart.empty:
                        st.bar_chart(kategori_chart)
                    else:
                        st.info("Kategori verisi yok.")
                except Exception as e:
                    st.warning(f"Kategori grafiği hatası: {e}")
            
            with col2:
                st.subheader("🏢 Firma Harcama")
                try:
                    firma_chart = temp_df.dropna(subset=['firma']).groupby("firma")["toplam_fiyat_sayi"].sum()
                    if not firma_chart.empty:
                        st.bar_chart(firma_chart)
                    else:
                        st.info("Firma verisi yok.")
                except Exception as e:
                    st.warning(f"Firma grafiği hatası: {e}")
            
        except Exception as e:
            st.warning(f"⚠️ Veriler gösterilirken hata: {e}")
    else:
        st.info("📭 Henüz veri yok. Lütfen fatura yükleyin.")

elif sayfa == "🧾 Komisyon Hesapla":
    ay_adi = calendar.month_name[secilen_ay_num]
    st.title(f"🧾 {ay_adi} {secilen_yil} Trendyol Yemek Komisyon Hesaplama")
    
    st.markdown("Trendyol Yemek için komisyon ve stopaj hesaplamasını yapabilirsiniz.")
    siparis_tutari = st.number_input("Sipariş Tutarı (TL)", min_value=0.0, value=100.0, step=1.0, format="%.2f")
    indirim = st.number_input("İndirim (TL)", min_value=0.0, value=0.0, step=1.0, format="%.2f")
    teslimat_model = st.selectbox("Teslimat Modeli", ["Trendyol GO", "Kendi Kuryem"])
    kurye_maliyeti = st.number_input("Kurye/Paket Maliyeti (TL)", min_value=0.0, value=10.0, step=1.0, format="%.2f")
    komisyon_orani = st.number_input("Komisyon Oranı (%)", min_value=0.0, max_value=100.0, value=12.0, step=0.1, format="%.2f")
    stopaj_orani = st.number_input("Gelir Vergisi Stopajı (%)", min_value=0.0, max_value=100.0, value=1.0, step=0.1, format="%.2f")

    net_satis = siparis_tutari - indirim
    komisyon = net_satis * (komisyon_orani / 100)
    stopaj = net_satis * (stopaj_orani / 100)
    toplam_maliyet = komisyon + stopaj + kurye_maliyeti
    net_restoran = net_satis - toplam_maliyet
    marj = (net_restoran / net_satis * 100) if net_satis > 0 else 0.0

    st.subheader("📌 Hesaplanan Net Kazanç")
    col1, col2 = st.columns(2)
    with col1:
        st.metric("Ara Toplam", f"{net_satis:.2f} TL")
        st.metric("Komisyon Kesintisi", f"{komisyon:.2f} TL")
    with col2:
        st.metric("Stopaj Kesintisi", f"{stopaj:.2f} TL")
        st.metric("Kurye/Paket Maliyeti", f"{kurye_maliyeti:.2f} TL")

    st.divider()
    st.metric("Restorana Kalan Net", f"{net_restoran:.2f} TL", delta=f"%{marj:.2f} marj")
    
    st.info("Trendyol GO seçildiğinde teslimat hizmeti Trendyol tarafından sağlanır; Kendi Kuryem seçildiğinde kurye maliyeti restoran tarafından karşılanır.")

elif sayfa == "📚 Tüm Veriler":
    st.title(f"📚 {calendar.month_name[secilen_ay_num]} {secilen_yil} Faturaları")
    st.dataframe(birikmis_veri, width='stretch')
