import json
import streamlit as st
from pathlib import Path

st.set_page_config(page_title="PURC Tariff Reckoner", page_icon="⚡", layout="centered")


def resolve_data_path() -> Path:
    base_dir = Path(__file__).parent
    candidate_paths = [
        base_dir / "data" / "tariffs_1998_2010.json",
        base_dir / "tariffs_1998_2010.json",
    ]

    for path in candidate_paths:
        if path.exists():
            return path

    raise FileNotFoundError(
        "Could not find the tariff data file. Checked: "
        + ", ".join(str(path) for path in candidate_paths)
    )


DATA_PATH = resolve_data_path()


# ---------------------------
# Data
# ---------------------------
def load_tariffs():
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


# ---------------------------
# Statutory payments
# ---------------------------
def billing_year(period_key: str) -> int | None:
    try:
        return int(period_key.split("_")[0])
    except Exception:
        return None


def levy_rate(period_key: str, customer_type: str) -> float:
    """
    For 2016–2026, apply a 5% levy on total energy charge
    for all customer categories.
    """
    year = billing_year(period_key)
    if year is None:
        return 0.0

    if 2016 <= year <= 2026:
        return 0.05
    return 0.0


def tax_rate(period_key: str, customer_type: str) -> float:
    """
    For 2016–2026, apply 20% tax on (energy + service charge)
    for all non-residential / non-residential-equivalent categories.
    """
    year = billing_year(period_key)
    if year is None:
        return 0.0

    if 2016 <= year <= 2026 and customer_type != "Residential":
        return 0.20
    return 0.0


def calc_statutory_payments(energy: float, service: float, period_key: str, customer_type: str) -> tuple[float, float, float]:
    levy = energy * levy_rate(period_key, customer_type)
    tax = (energy + service) * tax_rate(period_key, customer_type)
    total = levy + tax
    return float(levy), float(tax), float(total)


# ---------------------------
# Forward energy calculations
# ---------------------------
def calc_tiered_energy_after_lifeline(kwh: float, tiers: list[dict]) -> float:
    """
    tiers items: {from_kwh, to_kwh, rate}
    Assumption used in your newer tables (2024+):
      lifeline is a fixed block (0–30 or 0–50),
      tiers start at 31 or 51, etc.
    """
    if kwh <= 0:
        return 0.0

    energy = 0.0
    for t in tiers:
        start = int(t["from_kwh"])
        end = t["to_kwh"]
        rate = float(t["rate"])

        if kwh < start:
            continue

        if end is None:
            units = kwh - (start - 1)
        else:
            end = int(end)
            units = min(kwh, end) - (start - 1)

        if units > 0:
            energy += units * rate

    return float(energy)


def calc_residential_forward(kwh: float, res: dict) -> tuple[float, float]:
    """
    Returns: (energy, service_charge)
    Supports:
      fixed_block_0_30  (0–30)
      fixed_block_0_50  (0–50)
      tiers that start after the lifeline block
    """
    kwh = float(kwh)
    if kwh < 0:
        raise ValueError("kWh cannot be negative.")

    service = float(res.get("service_charge", 0.0))

    # Lifeline service charge (if you ever want to apply it specifically) is kept in JSON,
    # but your UI currently uses service_charge only. We leave it as-is.
    # service_lifeline = float(res.get("service_charge_lifeline", 0.0))

    energy = 0.0

    if "fixed_block_0_30" in res:
        lifeline_limit = 30
        lifeline_rate = float(res["fixed_block_0_30"])
    elif "fixed_block_0_50" in res:
        lifeline_limit = 50
        lifeline_rate = float(res["fixed_block_0_50"])
    else:
        lifeline_limit = 0
        lifeline_rate = 0.0

    if lifeline_limit > 0:
        lifeline_units = min(kwh, lifeline_limit)
        energy += lifeline_units * lifeline_rate

        if kwh > lifeline_limit:
            energy += calc_tiered_energy_after_lifeline(kwh, res.get("tiers", []))
    else:
        # No lifeline block, treat tiers as starting from 0/1 based on your JSON
        # If your old-year JSON uses from_kwh: 0, you likely mean first unit.
        # This still works if tiers are properly defined.
        energy += calc_tiered_energy_after_lifeline(kwh, res.get("tiers", []))

    return float(energy), float(service)


def calc_non_residential_forward(kwh: float, nonres: dict) -> tuple[float, float]:
    """
    Returns: (energy, service_charge)
    For older/newer tables, tiers are used.
    If a tier starts from 0, we interpret it as starting at 1 for kWh counting.
    """
    kwh = float(kwh)
    if kwh < 0:
        raise ValueError("kWh cannot be negative.")

    tiers = []
    for t in nonres.get("tiers", []):
        start = int(t["from_kwh"])
        if start == 0:
            start = 1
        tiers.append({"from_kwh": start, "to_kwh": t["to_kwh"], "rate": t["rate"]})

    energy = calc_tiered_energy_after_lifeline(kwh, tiers)
    service = float(nonres.get("service_charge", 0.0))
    return float(energy), float(service)


def calc_slt_forward(kwh: float, demand_kva: float, slt: dict) -> tuple[float, float]:
    """
    Works for both:
      old SLT with max_demand_rate + energy_rate + service_charge
      new SLT (2024+) with energy_rate + service_charge only
    """
    kwh = float(kwh)
    demand_kva = float(demand_kva or 0.0)

    energy_rate = float(slt.get("energy_rate", 0.0))
    service = float(slt.get("service_charge", 0.0))

    max_demand_rate = float(slt.get("max_demand_rate", 0.0))
    demand_charge = demand_kva * max_demand_rate

    energy = (kwh * energy_rate) + demand_charge
    return float(energy), float(service)


# ---------------------------
# Reverse (Amount -> kWh)
# ---------------------------
def invert_residential_energy_to_kwh(energy: float, res: dict) -> float:
    """
    Given ENERGY (not including levies and not including service charge),
    estimate kWh for residential using the same block logic.
    """
    energy = float(energy)
    if energy <= 0:
        return 0.0

    # Detect lifeline block
    if "fixed_block_0_30" in res:
        lifeline_limit = 30
        lifeline_rate = float(res["fixed_block_0_30"])
    elif "fixed_block_0_50" in res:
        lifeline_limit = 50
        lifeline_rate = float(res["fixed_block_0_50"])
    else:
        lifeline_limit = 0
        lifeline_rate = 0.0

    kwh = 0.0

    if lifeline_limit > 0 and lifeline_rate > 0:
        lifeline_energy_cap = lifeline_limit * lifeline_rate

        if energy <= lifeline_energy_cap:
            return energy / lifeline_rate

        # consume full lifeline
        kwh += lifeline_limit
        remaining_energy = energy - lifeline_energy_cap
    else:
        remaining_energy = energy

    # Now consume tiers in order
    tiers = res.get("tiers", [])
    for t in tiers:
        start = int(t["from_kwh"])
        end = t["to_kwh"]
        rate = float(t["rate"])

        if end is None:
            # open ended
            kwh += remaining_energy / rate
            remaining_energy = 0.0
            break

        end = int(end)
        units_in_tier = end - (start - 1)
        tier_energy_cap = units_in_tier * rate

        if remaining_energy <= tier_energy_cap:
            kwh += remaining_energy / rate
            remaining_energy = 0.0
            break

        # consume full tier
        kwh += units_in_tier
        remaining_energy -= tier_energy_cap

    return float(max(kwh, 0.0))


def invert_nonres_energy_to_kwh(energy: float, nonres: dict) -> float:
    """
    Given ENERGY (not including levies and not including service charge),
    estimate kWh for non-residential.
    """
    energy = float(energy)
    if energy <= 0:
        return 0.0

    kwh = 0.0
    remaining = energy

    tiers = []
    for t in nonres.get("tiers", []):
        start = int(t["from_kwh"])
        if start == 0:
            start = 1
        tiers.append({"from_kwh": start, "to_kwh": t["to_kwh"], "rate": t["rate"]})

    for t in tiers:
        start = int(t["from_kwh"])
        end = t["to_kwh"]
        rate = float(t["rate"])

        if end is None:
            kwh += remaining / rate
            remaining = 0.0
            break

        end = int(end)
        units_in_tier = end - (start - 1)
        tier_energy_cap = units_in_tier * rate

        if remaining <= tier_energy_cap:
            kwh += remaining / rate
            remaining = 0.0
            break

        kwh += units_in_tier
        remaining -= tier_energy_cap

    return float(max(kwh, 0.0))


def invert_slt_energy_to_kwh(energy: float, slt: dict, demand_kva: float) -> float:
    """
    Given ENERGY (not including service charge), invert SLT:
      energy = kwh*energy_rate + demand_kva*max_demand_rate
    """
    energy = float(energy)
    demand_kva = float(demand_kva or 0.0)

    energy_rate = float(slt.get("energy_rate", 0.0))
    max_demand_rate = float(slt.get("max_demand_rate", 0.0))

    demand_charge = demand_kva * max_demand_rate
    remaining_for_kwh = max(energy - demand_charge, 0.0)

    if energy_rate <= 0:
        return 0.0

    return float(remaining_for_kwh / energy_rate)


# ---------------------------
# UI
# ---------------------------
tariffs = load_tariffs()

if "result_energy" not in st.session_state:
    st.session_state.result_energy = ""
    st.session_state.result_service = ""
    st.session_state.result_levies = "0.00"
    st.session_state.result_total = ""
    st.session_state.result_kwh = ""


st.markdown(
    """
    <h1 style="margin-bottom:0.25rem;">PURC Tariff Reckoner</h1>
    """,
    unsafe_allow_html=True
)

st.write("")

# Select Tariff, Customer Type like your second screenshot
col1, col2 = st.columns(2)
with col1:
    period_key = st.selectbox("Select Tariff", list(tariffs.keys()))
with col2:
    # Customer type options: Residential, Non-Residential + whatever SLT keys exist in that period
    period = tariffs[period_key]
    slt_keys = sorted(list(period.get("slt", {}).keys()))
    customer_type = st.selectbox("Customer Type", ["Residential", "Non-Residential"] + slt_keys)

# Preference dropdown (kWh -> Total) or (Total -> kWh)
preference = st.selectbox(
    "Preference",
    ["Consumption (kWh) → Total Amount (GHS)", "Total Amount (GHS) → Consumption (kWh)"]
)

st.info(period.get("label", period_key))

# Inputs change depending on preference
demand_kva = 0.0
kwh = 0.0
total_amount = 0.0

if preference == "Consumption (kWh) → Total Amount (GHS)":
    kwh = st.number_input("Consumption (kWh)", min_value=0.0, value=200.0, step=1.0)
    if customer_type not in ["Residential", "Non-Residential"]:
        # Only show demand if that SLT record actually has a max demand rate
        slt_data = period["slt"][customer_type]
        if float(slt_data.get("max_demand_rate", 0.0)) > 0:
            demand_kva = st.number_input("Maximum Demand (kVA)", min_value=0.0, value=0.0, step=1.0)

else:
    total_amount = st.number_input("Total Amount (GHS)", min_value=0.0, value=10.75, step=0.01)
    if customer_type not in ["Residential", "Non-Residential"]:
        slt_data = period["slt"][customer_type]
        if float(slt_data.get("max_demand_rate", 0.0)) > 0:
            demand_kva = st.number_input("Maximum Demand (kVA)", min_value=0.0, value=0.0, step=1.0)

# Outputs
st.text_input("Energy Charge (GHS)", value=st.session_state.result_energy)
st.text_input("Levies/Taxes (GHS)", value=st.session_state.result_levies)
st.text_input("Service Charge (GHS)", value=st.session_state.result_service)
st.text_input("Total Amount (GHS)", value=st.session_state.result_total)

# Only show estimated kWh box when doing reverse
if preference == "Total Amount (GHS) → Consumption (kWh)":
    st.text_input("Estimated Consumption (kWh)", value=st.session_state.result_kwh)

if st.button("CALCULATE"):
    period = tariffs[period_key]

    if preference == "Consumption (kWh) → Total Amount (GHS)":
        # Forward
        if customer_type == "Residential":
            energy, service = calc_residential_forward(kwh, period["residential"])
        elif customer_type == "Non-Residential":
            energy, service = calc_non_residential_forward(kwh, period["non_residential"])
        else:
            slt_data = period["slt"][customer_type]
            energy, service = calc_slt_forward(kwh, demand_kva, slt_data)

        levy, tax, statutory_total = calc_statutory_payments(energy, service, period_key, customer_type)
        total = energy + service + statutory_total

        st.session_state.result_energy = f"{energy:,.2f}"
        st.session_state.result_service = f"{service:,.2f}"
        st.session_state.result_levies = f"{statutory_total:,.2f}"
        st.session_state.result_total = f"{total:,.2f}"
        st.session_state.result_kwh = ""

    else:
        # Reverse: Total -> kWh
        # Residential (2016-2026):
        #   total = energy + levy + service = energy*(1+r) + service
        # All other categories (2016-2026):
        #   total = energy + levy + service + tax
        #         = energy*(1+r+t) + service*(1+t)
        r = levy_rate(period_key, customer_type)
        t = tax_rate(period_key, customer_type)

        # determine service charge first (depends on customer type)
        if customer_type == "Residential":
            service = float(period["residential"].get("service_charge", 0.0))
        elif customer_type == "Non-Residential":
            service = float(period["non_residential"].get("service_charge", 0.0))
        else:
            slt_data = period["slt"][customer_type]
            service = float(slt_data.get("service_charge", 0.0))

        net_for_energy = max(float(total_amount) - (service * (1.0 + t)), 0.0)

        energy_multiplier = 1.0 + r + t
        if energy_multiplier <= 0:
            energy = 0.0
        else:
            energy = net_for_energy / energy_multiplier

        levy, tax, statutory_total = calc_statutory_payments(energy, service, period_key, customer_type)

        # invert energy -> kWh
        if customer_type == "Residential":
            kwh_est = invert_residential_energy_to_kwh(energy, period["residential"])
        elif customer_type == "Non-Residential":
            kwh_est = invert_nonres_energy_to_kwh(energy, period["non_residential"])
        else:
            slt_data = period["slt"][customer_type]
            kwh_est = invert_slt_energy_to_kwh(energy, slt_data, demand_kva)

        total_check = energy + service + statutory_total

        st.session_state.result_energy = f"{energy:,.2f}"
        st.session_state.result_service = f"{service:,.2f}"
        st.session_state.result_levies = f"{statutory_total:,.2f}"
        st.session_state.result_total = f"{total_check:,.2f}"
        st.session_state.result_kwh = f"{kwh_est:,.2f}"
