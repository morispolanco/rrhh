import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st


# =========================================================
# Configuración general
# =========================================================
st.set_page_config(
    page_title="RRHH Guatemala | Evaluación de Personal",
    page_icon="👥",
    layout="wide",
)

DB_PATH = os.getenv("HR_APP_DB", "hr_evaluacion_guatemala.db")

st.title("👥 Evaluación Integral de Personal")
st.caption(
    "Aplicación de recursos humanos adaptada al contexto de Guatemala: idoneidad, permanencia,"
    " ajuste cultural y riesgo de rotación."
)


# =========================================================
# Modelos de datos
# =========================================================
@dataclass
class JobProfile:
    puesto: str
    sector: str
    ubicacion: str
    zona_rural: bool
    nivel_educativo_requerido: str
    habilidades_tecnicas_requeridas: List[str]
    habilidades_blandas_requeridas: List[str]
    salario_ofrecido: float
    horario: str
    tipo_contrato: str
    modalidad: str


@dataclass
class CandidateProfile:
    nombre: str
    nivel_educativo: str
    experiencia_anios: float
    habilidades_tecnicas: List[str]
    habilidades_blandas: List[str]
    residencia: str
    distancia_km: float
    expectativa_salarial: float
    cambios_empleo_3anios: int
    meses_en_ultimo_empleo: int
    disponibilidad_horario: str
    acepta_turnos: bool
    acepta_trabajo_rural: bool


# =========================================================
# Utilidades
# =========================================================
def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def parse_list(text: str) -> List[str]:
    return [x.strip() for x in text.replace("\n", ",").split(",") if x.strip()]


def normalize_text(value: str) -> str:
    return value.strip().lower()


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash


def overlap_score(candidate_items: List[str], required_items: List[str]) -> float:
    if not required_items:
        return 100.0
    c = {normalize_text(x) for x in candidate_items if x.strip()}
    r = {normalize_text(x) for x in required_items if x.strip()}
    if not r:
        return 100.0
    return 100.0 * len(c.intersection(r)) / len(r)


def education_score(candidate_level: str, required_level: str) -> float:
    order = {
        "Primaria": 1,
        "Básico": 2,
        "Diversificado": 3,
        "Técnico": 4,
        "Universitario": 5,
        "Posgrado": 6,
    }
    c = order.get(candidate_level, 0)
    r = order.get(required_level, 0)
    if c == 0 or r == 0:
        return 50.0
    if c >= r:
        return 100.0
    if c == r - 1:
        return 70.0
    return 35.0


def salary_fit_score(expected_salary: float, offered_salary: float) -> float:
    if expected_salary <= 0 or offered_salary <= 0:
        return 50.0
    ratio = offered_salary / expected_salary
    if ratio >= 1.15:
        return 100.0
    if ratio >= 1.0:
        return 88.0
    if ratio >= 0.9:
        return 72.0
    if ratio >= 0.8:
        return 55.0
    if ratio >= 0.7:
        return 35.0
    return 15.0


def distance_penalty_km(distance_km: float, rural: bool) -> float:
    if distance_km <= 3:
        base = 0
    elif distance_km <= 10:
        base = 4
    elif distance_km <= 20:
        base = 9
    elif distance_km <= 35:
        base = 15
    else:
        base = 22
    if rural:
        base *= 1.2
    return base


def tenure_risk_score(job_changes_3y: int, months_last_job: int) -> float:
    risk = 0.0
    if job_changes_3y <= 1:
        risk += 10
    elif job_changes_3y <= 2:
        risk += 22
    elif job_changes_3y <= 4:
        risk += 45
    else:
        risk += 65

    if months_last_job < 6:
        risk += 25
    elif months_last_job < 12:
        risk += 12
    elif months_last_job < 24:
        risk += 5

    return clamp(risk, 0, 100)


def color_label(score: float) -> str:
    if score >= 80:
        return "Verde"
    if score >= 60:
        return "Amarillo"
    return "Rojo"


def explain_score(score: float) -> str:
    if score >= 80:
        return "Alta"
    if score >= 60:
        return "Media"
    return "Baja"


def produce_recommendation(overall: float, permanency: float, fit_cultural: float, risk: float) -> str:
    if overall >= 80 and permanency >= 70 and risk < 35:
        return "Recomendado para contratación inmediata, con seguimiento inicial de 30 días."
    if overall >= 70 and permanency >= 60:
        return "Candidato sólido, pero conviene validar referencias y ajustar expectativas del puesto."
    if overall >= 55:
        return "Candidato intermedio: podría funcionar con capacitación o adaptación del puesto."
    return "No priorizar en esta vacante; la brecha entre perfil y puesto es alta."


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            username TEXT NOT NULL,
            candidate_name TEXT NOT NULL,
            job_title TEXT NOT NULL,
            result_json TEXT NOT NULL
        )
        """
    )
    conn.commit()

    # Usuario administrador inicial desde secrets o valores por defecto.
    admin_user = st.secrets.get("admin_username", os.getenv("ADMIN_USERNAME", "admin"))
    admin_pass = st.secrets.get("admin_password", os.getenv("ADMIN_PASSWORD", "admin123"))
    cur.execute("SELECT COUNT(*) FROM users WHERE username = ?", (admin_user,))
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (admin_user, hash_password(admin_pass), "admin", datetime.utcnow().isoformat()),
        )
        conn.commit()
    return conn


conn = init_db()


def get_user(username: str) -> Optional[Tuple[int, str, str, str, str]]:
    cur = conn.cursor()
    cur.execute("SELECT id, username, password_hash, role, created_at FROM users WHERE username = ?", (username,))
    return cur.fetchone()


def create_user(username: str, password: str, role: str = "user") -> Tuple[bool, str]:
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), role, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return True, "Usuario creado correctamente."
    except sqlite3.IntegrityError:
        return False, "El usuario ya existe."
    except Exception as e:
        return False, f"Error al crear usuario: {e}"


def save_evaluation(username: str, candidate_name: str, job_title: str, result: Dict) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO evaluations (created_at, username, candidate_name, job_title, result_json) VALUES (?, ?, ?, ?, ?)",
        (datetime.utcnow().isoformat(), username, candidate_name, job_title, json.dumps(result, ensure_ascii=False)),
    )
    conn.commit()


def list_evaluations() -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT created_at, username, candidate_name, job_title, result_json FROM evaluations ORDER BY id DESC",
        conn,
    )
    if df.empty:
        return df
    parsed = df["result_json"].apply(json.loads)
    expanded = pd.json_normalize(parsed)
    out = pd.concat([df.drop(columns=["result_json"]), expanded], axis=1)
    return out


# =========================================================
# Autenticación
# =========================================================
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "current_user" not in st.session_state:
    st.session_state.current_user = None
if "current_role" not in st.session_state:
    st.session_state.current_role = None


if not st.session_state.authenticated:
    st.subheader("Acceso al sistema")
    c1, c2 = st.columns(2)
    with c1:
        login_user = st.text_input("Usuario", key="login_user")
    with c2:
        login_pass = st.text_input("Contraseña", type="password", key="login_pass")

    if st.button("Ingresar", type="primary"):
        record = get_user(login_user)
        if record and verify_password(login_pass, record[2]):
            st.session_state.authenticated = True
            st.session_state.current_user = record[1]
            st.session_state.current_role = record[3]
            st.rerun()
        else:
            st.error("Credenciales inválidas.")

    st.stop()


with st.sidebar:
    st.success(f"Sesión activa: {st.session_state.current_user} ({st.session_state.current_role})")
    if st.button("Cerrar sesión"):
        st.session_state.authenticated = False
        st.session_state.current_user = None
        st.session_state.current_role = None
        st.rerun()


# =========================================================
# Capa opcional de IA
# =========================================================
def generate_ai_summary(payload: Dict) -> str:
    """Intenta usar una API externa si hay configuración en secrets.
    Compatible con un estilo sencillo: endpoint, api_key y model.
    """
    api_key = st.secrets.get("llm_api_key", os.getenv("LLM_API_KEY", ""))
    api_url = st.secrets.get("llm_api_url", os.getenv("LLM_API_URL", ""))
    api_model = st.secrets.get("llm_model", os.getenv("LLM_MODEL", ""))

    if not api_key or not api_url:
        return (
            "La evaluación se generó con reglas internas. Para activación de IA, configure llm_api_key, "
            "llm_api_url y llm_model en secrets o variables de entorno."
        )

    prompt = f"""
Eres un analista de recursos humanos en Guatemala.
Redacta un resumen ejecutivo breve, claro y profesional sobre el siguiente candidato.
Debes incluir: idoneidad, permanencia estimada, ajuste cultural, riesgos y recomendación final.

Datos:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    body = {
        "model": api_model,
        "messages": [
            {"role": "system", "content": "Eres un asistente experto en RRHH."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }

    try:
        r = requests.post(api_url, headers=headers, json=body, timeout=60)
        r.raise_for_status()
        data = r.json()

        # Soporte para respuestas tipo OpenAI-style
        if isinstance(data, dict):
            if "choices" in data and data["choices"]:
                msg = data["choices"][0].get("message", {}).get("content")
                if msg:
                    return msg.strip()
            if "output_text" in data:
                return str(data["output_text"]).strip()

        return "No se pudo interpretar la respuesta de la API de IA."
    except Exception as e:
        return f"No fue posible consultar la API de IA: {e}"


# =========================================================
# Entradas del puesto y del candidato
# =========================================================
with st.sidebar:
    st.header("Perfil del puesto")
    puesto = st.text_input("Nombre del puesto", value="Asistente administrativo")
    sector = st.selectbox(
        "Sector económico",
        ["Servicios", "Comercio", "Industria", "Agrícola", "Tecnología", "Salud", "Educación", "Otro"],
    )
    ubicacion = st.text_input("Ubicación del puesto", value="Ciudad de Guatemala")
    zona_rural = st.checkbox("El puesto está en zona rural", value=False)
    nivel_educativo_requerido = st.selectbox(
        "Nivel educativo mínimo",
        ["Primaria", "Básico", "Diversificado", "Técnico", "Universitario", "Posgrado"],
        index=2,
    )
    habilidades_tecnicas_requeridas = st.text_area(
        "Habilidades técnicas requeridas (coma)",
        value="Excel, atención al cliente, redacción, facturación",
    )
    habilidades_blandas_requeridas = st.text_area(
        "Habilidades blandas requeridas (coma)",
        value="responsabilidad, puntualidad, adaptabilidad, trabajo en equipo",
    )
    salario_ofrecido = st.number_input("Salario ofrecido (Q)", min_value=0.0, value=4500.0, step=100.0)
    horario = st.selectbox("Horario", ["Diurno", "Mixto", "Nocturno", "Rotativo"])
    tipo_contrato = st.selectbox("Tipo de contrato", ["Indefinido", "Plazo fijo", "Temporal", "Por servicios"])
    modalidad = st.selectbox("Modalidad", ["Presencial", "Híbrido", "Remoto"])

    st.divider()
    st.header("Perfil del candidato")
    nombre = st.text_input("Nombre del candidato", value="Juan Pérez")
    nivel_educativo = st.selectbox(
        "Nivel educativo del candidato",
        ["Primaria", "Básico", "Diversificado", "Técnico", "Universitario", "Posgrado"],
        index=2,
    )
    experiencia_anios = st.slider("Experiencia laboral (años)", 0.0, 30.0, 3.0, 0.5)
    habilidades_tecnicas = st.text_area(
        "Habilidades técnicas del candidato (coma)",
        value="Excel, redacción, atención al cliente",
    )
    habilidades_blandas = st.text_area(
        "Habilidades blandas del candidato (coma)",
        value="responsabilidad, puntualidad, trabajo en equipo",
    )
    residencia = st.text_input("Lugar de residencia", value="Mixco")
    distancia_km = st.number_input("Distancia estimada al trabajo (km)", min_value=0.0, value=8.0, step=1.0)
    expectativa_salarial = st.number_input("Expectativa salarial (Q)", min_value=0.0, value=4000.0, step=100.0)
    cambios_empleo_3anios = st.number_input("Cambios de empleo en 3 años", min_value=0, max_value=20, value=2, step=1)
    meses_en_ultimo_empleo = st.number_input("Meses en el último empleo", min_value=0, max_value=240, value=14, step=1)
    disponibilidad_horario = st.selectbox("Disponibilidad horaria", ["Completa", "Diurna", "Mixta", "Nocturna", "Limitada"])
    acepta_turnos = st.checkbox("Acepta turnos rotativos", value=False)
    acepta_trabajo_rural = st.checkbox("Acepta trabajo rural", value=False)

    st.divider()
    st.header("Ponderaciones")
    peso_idoneidad = st.slider("Peso idoneidad", 0, 100, 45)
    peso_permanencia = st.slider("Peso permanencia", 0, 100, 30)
    peso_cultural = st.slider("Peso ajuste cultural", 0, 100, 15)
    peso_riesgo = st.slider("Peso riesgo negativo", 0, 100, 10)

    evaluar = st.button("Evaluar candidato", type="primary", use_container_width=True)


# =========================================================
# Cálculos principales
# =========================================================
job = JobProfile(
    puesto=puesto,
    sector=sector,
    ubicacion=ubicacion,
    zona_rural=zona_rural,
    nivel_educativo_requerido=nivel_educativo_requerido,
    habilidades_tecnicas_requeridas=parse_list(habilidades_tecnicas_requeridas),
    habilidades_blandas_requeridas=parse_list(habilidades_blandas_requeridas),
    salario_ofrecido=salario_ofrecido,
    horario=horario,
    tipo_contrato=tipo_contrato,
    modalidad=modalidad,
)

candidate = CandidateProfile(
    nombre=nombre,
    nivel_educativo=nivel_educativo,
    experiencia_anios=experiencia_anios,
    habilidades_tecnicas=parse_list(habilidades_tecnicas),
    habilidades_blandas=parse_list(habilidades_blandas),
    residencia=residencia,
    distancia_km=distancia_km,
    expectativa_salarial=expectativa_salarial,
    cambios_empleo_3anios=int(cambios_empleo_3anios),
    meses_en_ultimo_empleo=int(meses_en_ultimo_empleo),
    disponibilidad_horario=disponibilidad_horario,
    acepta_turnos=acepta_turnos,
    acepta_trabajo_rural=acepta_trabajo_rural,
)

edu = education_score(candidate.nivel_educativo, job.nivel_educativo_requerido)
tech = overlap_score(candidate.habilidades_tecnicas, job.habilidades_tecnicas_requeridas)
soft = overlap_score(candidate.habilidades_blandas, job.habilidades_blandas_requeridas)
experience_score = clamp((candidate.experiencia_anios / 5.0) * 100.0)
experience_score = 100.0 if candidate.experiencia_anios >= 5 else max(25.0, experience_score)

turno_penalty = 0.0
if job.horario in ["Nocturno", "Rotativo"] and not candidate.acepta_turnos:
    turno_penalty = 15.0
if job.zona_rural and not candidate.acepta_trabajo_rural:
    turno_penalty += 10.0

salary_score = salary_fit_score(candidate.expectativa_salarial, job.salario_ofrecido)
route_penalty = distance_penalty_km(candidate.distancia_km, job.zona_rural)
route_score = clamp(100.0 - route_penalty)
risk_base = tenure_risk_score(candidate.cambios_empleo_3anios, candidate.meses_en_ultimo_empleo)

permanency_score = clamp(
    100.0
    - (0.40 * risk_base)
    + (0.25 * salary_score)
    + (0.20 * route_score)
    - (0.25 * turno_penalty)
)

cultural_score = clamp(
    0.35 * soft
    + 0.20 * route_score
    + 0.15 * (100.0 if candidate.disponibilidad_horario == "Completa" else 70.0)
    + 0.15 * (100.0 if job.modalidad == "Remoto" else 85.0)
    + 0.15 * (100.0 if job.sector in ["Servicios", "Comercio"] else 78.0)
)

idoneity_score = clamp(
    0.25 * edu + 0.30 * tech + 0.15 * soft + 0.20 * experience_score + 0.10 * salary_score
    - turno_penalty * 0.35
)

risk_score = clamp(
    0.45 * risk_base + 0.20 * (100.0 - salary_score) + 0.15 * (100.0 - route_score)
    + 0.20 * turno_penalty
)

weighted_total = clamp(
    (peso_idoneidad / 100) * idoneity_score
    + (peso_permanencia / 100) * permanency_score
    + (peso_cultural / 100) * cultural_score
    - (peso_riesgo / 100) * risk_score * 0.6
)

recommendation = produce_recommendation(weighted_total, permanency_score, cultural_score, risk_score)

alerts: List[str] = []
if salary_score < 60:
    alerts.append("La expectativa salarial del candidato está por encima del salario ofrecido.")
if route_score < 70:
    alerts.append("La distancia o traslado puede afectar puntualidad y permanencia.")
if risk_base > 50:
    alerts.append("El historial reciente muestra posible rotación laboral elevada.")
if turno_penalty > 0:
    alerts.append("Existe una incompatibilidad parcial con el horario o la modalidad del puesto.")
if edu < 60:
    alerts.append("El nivel educativo está por debajo de lo requerido para esta vacante.")

strengths: List[str] = []
if tech >= 70:
    strengths.append("Buena coincidencia técnica.")
if soft >= 70:
    strengths.append("Fortaleza en habilidades blandas.")
if salary_score >= 80:
    strengths.append("La expectativa salarial es compatible con la oferta.")
if route_score >= 80:
    strengths.append("Traslado razonable para el puesto.")
if permanency_score >= 75:
    strengths.append("Perfil favorable para permanencia.")


def build_result_payload() -> Dict:
    return {
        "candidato": asdict(candidate),
        "puesto": asdict(job),
        "scores": {
            "idoneidad": round(idoneity_score, 2),
            "permanencia": round(permanency_score, 2),
            "cultural": round(cultural_score, 2),
            "riesgo": round(risk_score, 2),
            "resultado_global": round(weighted_total, 2),
        },
        "recomendacion": recommendation,
        "alertas": alerts,
        "fortalezas": strengths,
    }


def validate_username(username: str) -> Tuple[bool, str]:
    username = username.strip()
    if len(username) < 3:
        return False, "El usuario debe tener al menos 3 caracteres."
    if " " in username:
        return False, "El usuario no debe contener espacios."
    return True, ""


def register_user(username: str, password: str, confirm_password: str, role: str = "user") -> Tuple[bool, str]:
    ok, msg = validate_username(username)
    if not ok:
        return False, msg
    if len(password) < 6:
        return False, "La contraseña debe tener al menos 6 caracteres."
    if password != confirm_password:
        return False, "Las contraseñas no coinciden."
    return create_user(username.strip(), password, role)


# =========================================================
# Interfaz principal
# =========================================================
tab1, tab2, tab3 = st.tabs(["Evaluación", "Historial", "Administración"])

with tab1:
    if evaluar:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Idoneidad", f"{idoneity_score:.0f}%", delta=color_label(idoneity_score))
        col2.metric("Permanencia estimada", f"{permanency_score:.0f}%", delta=explain_score(permanency_score))
        col3.metric("Ajuste cultural", f"{cultural_score:.0f}%", delta=explain_score(cultural_score))
        col4.metric("Riesgo de rotación", f"{risk_score:.0f}%", delta=color_label(100 - risk_score))

        def gauge(value: float, title: str, threshold: float = 70.0):
            fig = go.Figure(
                go.Indicator(
                    mode="gauge+number",
                    value=value,
                    number={"suffix": "%"},
                    title={"text": title},
                    gauge={
                        "axis": {"range": [0, 100]},
                        "bar": {"color": "black"},
                        "steps": [
                            {"range": [0, 50], "color": "#f8d7da"},
                            {"range": [50, 80], "color": "#fff3cd"},
                            {"range": [80, 100], "color": "#d1e7dd"},
                        ],
                        "threshold": {"line": {"color": "red", "width": 4}, "thickness": 0.75, "value": threshold},
                    },
                )
            )
            fig.update_layout(height=280, margin=dict(l=20, r=20, t=40, b=20))
            return fig

        g1, g2 = st.columns(2)
        with g1:
            st.plotly_chart(gauge(weighted_total, "Resultado global"), use_container_width=True)
        with g2:
            st.plotly_chart(gauge(permanency_score, "Permanencia estimada"), use_container_width=True)

        st.subheader("Resumen ejecutivo")
        st.success(recommendation)

        left, right = st.columns([1.1, 0.9])
        with left:
            st.write("### Desglose del análisis")
            df = pd.DataFrame(
                [
                    ["Nivel educativo", edu],
                    ["Habilidades técnicas", tech],
                    ["Habilidades blandas", soft],
                    ["Experiencia", experience_score],
                    ["Compatibilidad salarial", salary_score],
                    ["Ajuste por traslado", route_score],
                    ["Ajuste horario", max(0, 100 - turno_penalty * 5)],
                    ["Permanencia estimada", permanency_score],
                    ["Ajuste cultural", cultural_score],
                    ["Riesgo total", risk_score],
                ],
                columns=["Factor", "Puntaje"],
            )
            st.dataframe(df, use_container_width=True, hide_index=True)

        with right:
            st.write("### Fortalezas y alertas")
            st.markdown("**Fortalezas**")
            if strengths:
                for item in strengths:
                    st.markdown(f"- {item}")
            else:
                st.markdown("- No se detectan fortalezas claras con la información ingresada.")

            st.markdown("**Alertas**")
            if alerts:
                for item in alerts:
                    st.markdown(f"- {item}")
            else:
                st.markdown("- No se detectan alertas relevantes.")

        st.subheader("Visualización comparativa")
        fig_radar = go.Figure()
        fig_radar.add_trace(
            go.Scatterpolar(
                r=[idoneity_score, permanency_score, cultural_score, 100 - risk_score, idoneity_score],
                theta=["Idoneidad", "Permanencia", "Ajuste cultural", "Bajo riesgo", "Idoneidad"],
                fill="toself",
                name="Candidato",
            )
        )
        fig_radar.update_layout(
            polar=dict(radialaxis=dict(visible=True, range=[0, 100])),
            showlegend=False,
            height=420,
            margin=dict(l=40, r=40, t=40, b=40),
        )
        st.plotly_chart(fig_radar, use_container_width=True)

        st.subheader("Sugerencias automáticas")
        suggestions: List[str] = []
        if salary_score < 80:
            suggestions.append("Revisar banda salarial o incluir incentivos por desempeño/traslado.")
        if route_score < 80:
            suggestions.append("Considerar apoyo de transporte o un esquema híbrido si el puesto lo permite.")
        if tech < 70:
            suggestions.append("Programar una prueba técnica breve o un plan de capacitación de ingreso.")
        if soft < 70:
            suggestions.append("Aplicar entrevista por competencias enfocada en puntualidad, servicio y trabajo en equipo.")
        if risk_score >= 50:
            suggestions.append("Validar referencias laborales y motivos de salida de empleos anteriores.")
        if job.zona_rural and not candidate.acepta_trabajo_rural:
            suggestions.append("Explorar candidato alterno con mayor disposición a traslado en zona rural.")
        if not suggestions:
            suggestions.append("El perfil es compatible con la vacante. Proceder a entrevista final.")
        for s in suggestions:
            st.markdown(f"- {s}")

        st.subheader("Resumen para guardar")
        summary_df = pd.DataFrame(
            [
                {
                    "Candidato": candidate.nombre,
                    "Puesto": job.puesto,
                    "Idoneidad": round(idoneity_score, 1),
                    "Permanencia": round(permanency_score, 1),
                    "Ajuste cultural": round(cultural_score, 1),
                    "Riesgo": round(risk_score, 1),
                    "Resultado global": round(weighted_total, 1),
                    "Decisión": recommendation,
                }
            ]
        )
        st.dataframe(summary_df, use_container_width=True, hide_index=True)

        csv = summary_df.to_csv(index=False).encode("utf-8")
        st.download_button("Descargar resumen CSV", csv, file_name="evaluacion_personal_guatemala.csv", mime="text/csv")

        if st.button("Guardar evaluación en la base de datos"):
            payload = build_result_payload()
            save_evaluation(st.session_state.current_user, candidate.nombre, job.puesto, payload)
            st.success("Evaluación guardada correctamente.")

        st.subheader("Resumen generado por IA")
        ai_output = generate_ai_summary(build_result_payload())
        st.info(ai_output)
    else:
        st.info("Completa el formulario y pulsa **Evaluar candidato** para generar el análisis.")
        st.markdown("### Qué distingue esta aplicación")
        st.markdown(
            "- No solo analiza currículums: evalúa **idoneidad, permanencia y ajuste al contexto guatemalteco**.\n"
            "- Considera **salario en quetzales, distancia, horario, traslado y rotación laboral**.\n"
            "- Permite guardar resultados y generar un resumen ejecutivo.")

with tab2:
    st.subheader("Historial de evaluaciones")
    history = list_evaluations()
    if history.empty:
        st.info("Aún no hay evaluaciones registradas.")
    else:
        cols_to_show = [c for c in ["created_at", "username", "candidate_name", "job_title", "scores.resultado_global", "scores.idoneidad", "scores.permanencia", "scores.cultural", "scores.riesgo", "recomendacion"] if c in history.columns]
        st.dataframe(history[cols_to_show], use_container_width=True, hide_index=True)
        csv_history = history.to_csv(index=False).encode("utf-8")
        st.download_button("Descargar historial CSV", csv_history, file_name="historial_evaluaciones.csv", mime="text/csv")

with tab3:
    st.subheader("Registro de usuarios")
    if st.session_state.current_role != "admin":
        st.warning("Solo el administrador puede crear usuarios.")
    else:
        reg_col1, reg_col2 = st.columns(2)
        with reg_col1:
            new_user = st.text_input("Nuevo usuario", key="new_user")
            new_role = st.selectbox("Rol", ["user", "admin"], key="new_role")
        with reg_col2:
            new_pass = st.text_input("Nueva contraseña", type="password", key="new_pass")
            confirm_pass = st.text_input("Confirmar contraseña", type="password", key="confirm_pass")

        if st.button("Registrar usuario"):
            ok, msg = register_user(new_user, new_pass, confirm_pass, new_role)
            if ok:
                st.success(msg)
            else:
                st.error(msg)

        st.markdown("#### Usuarios registrados")
        users_df = pd.read_sql_query("SELECT username, role, created_at FROM users ORDER BY id DESC", conn)
        if users_df.empty:
            st.info("Todavía no hay usuarios registrados.")
        else:
            st.dataframe(users_df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Configuración técnica")
    st.code(
        """
Se recomienda configurar en secrets.toml o variables de entorno:

admin_username = "admin"
admin_password = "admin123"
llm_api_key = "..."
llm_api_url = "..."
llm_model = "..."
        """.strip(),
        language="text",
    )


# =========================================================
# Pie de página
# =========================================================
st.divider()
st.caption(
    "Nota: este modelo apoya la decisión de RRHH, pero no sustituye entrevistas, verificación de referencias"
    " ni validaciones legales o laborales."
)
