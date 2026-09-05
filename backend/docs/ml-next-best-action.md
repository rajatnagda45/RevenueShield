# Machine Learning Next-Best-Action Formulation

## 1. Problem Definition

Given an open recovery case $c$, customer profile $u$, current observation context $x_t \in \mathcal{X}$, and a set of candidate recovery interventions $\mathcal{A}$:

$$\mathcal{A} = \{ \text{EMAIL\_PAYMENT\_RECOVERY}, \text{EMAIL\_FOLLOWUP}, \text{WHATSAPP\_PAYMENT\_RECOVERY}, \dots \}$$

The objective is to choose an optimal action $a^* \in \mathcal{A}$ that maximizes expected recovered revenue:

$$a^* = \arg\max_{a \in \mathcal{A}_{\text{allowed}}} \mathbb{E}[\text{Recovered Revenue} \mid x_t, a]$$

where $\mathcal{A}_{\text{allowed}} \subseteq \mathcal{A}$ is the filtered subset of actions that satisfy all **HARD safety and compliance policies** (e.g., DND quiet hours, opt-outs, attempt caps, Promise-to-Pay).

---

## 2. Expected Recovery Value (EV) Calculation

The expected recovered revenue for candidate action $a$ is computed as:

$$\text{EV}(a) = P(\text{recovered} = 1 \mid x_t, a) \times \text{Amount at Risk}$$

### Example:
- **Case**: Failed SaaS invoice of ₹10,000.
- **Candidate Actions**:
  - $a_1 = \text{EMAIL\_PAYMENT\_RECOVERY} \implies P(\text{rec} \mid a_1) = 0.42 \implies \text{EV} = ₹4,200$
  - $a_2 = \text{EMAIL\_FOLLOWUP} \implies P(\text{rec} \mid a_2) = 0.65 \implies \text{EV} = ₹6,500$
- **Decision**: Select $a_2$ (Expected value is higher by ₹2,300).

---

## 3. Probability Calibration

Because the decision threshold directly multiplies the estimated probability $P(\text{recovery})$ by currency values, raw uncalibrated probabilities produce distorted expected values.

We calibrate models using **Sigmoid Calibration (Platt Scaling)** or **Isotonic Regression** via `CalibratedClassifierCV`. Model quality is evaluated using:
1. **Brier Score**: Mean squared error of predicted probabilities:
   $$\text{Brier} = \frac{1}{N} \sum_{i=1}^N (P_i - y_i)^2$$
2. **Log Loss**: Cross-entropy error heavily penalizing confident incorrect predictions:
   $$\text{Log Loss} = -\frac{1}{N} \sum_{i=1}^N [y_i \ln P_i + (1 - y_i) \ln(1 - P_i)]$$
3. **ROC-AUC**: Discriminative rank-ordering capacity.

---

## 4. Policy as a Hard Constraint

Machine learning estimates value, but **policy sets hard boundaries**.
Even if a model predicts $P(\text{recovery} \mid \text{Voice}) = 0.95$, if the customer is on DND or reached the 3-attempt maximum, Voice is **BLOCKED** and cannot be selected.

---

## 5. Graceful Fallback Guarantee

If ML inference fails, dataset sample count is below `MIN_TRAINING_SAMPLES = 50`, or model drift is detected, the sequencer seamlessly falls back to `RuleBasedActionPolicy` and records `MODEL_FALLBACK_USED` in the audit log. The recovery engine never halts execution.
