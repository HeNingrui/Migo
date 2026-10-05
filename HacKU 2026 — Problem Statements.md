# ![][image1] 2026 — Problem Statements

### **README**

Every statement below uses the same four fields: Challenge, Scope requirement, Potential directions, Evidence.

The Potential Directions are provided as examples only; you don't have to follow them and are free to come up with your own. You should define the user, the setting, the data and the method.

1. ### **Financial Technology Problem Statement: Give a Machine a Wallet – Agentic Commerce (sponsored by HKT)**

**CHALLENGE**  
Design and build an intelligent, user-centric Shopping Agent that transforms the digital retail experience. Your solution should help consumers discover and compare products, recommend the best payment, maximize rewards and loyalty benefits, and complete purchases seamlessly. How can your agent solve a specific pain point in the modern e-commerce journey?

**SCOPE REQUIREMENT**  
Choose one spending decision and one party delegating it. Define what the agent may do, what it may not, and how a limit is actually enforced — a cap, a mandate, an expiry, a rule, a revocation. Demonstrate one complete transaction end to end, including at least one case where the agent is stopped. Payment and rewards sit inside this territory: how a payment route is chosen, and how reward or loyalty value is accounted for, are both legitimate centres for the work. You choose the user, the method and the interface. This statement does not prescribe one.

**POTENTIAL DIRECTIONS**

* How the authority is written  
  * A mandate a non-programmer can write — a sentence, a form, a signed policy file — and what it takes to turn that into something enforceable.  
  * A cap that adapts: per transaction, per day, per merchant, or a rolling window; and the case where a flat cap is not enough.  
  * A mandate with an expiry, and a renewal that needs a person to act.  
  * A delegation chain — A authorises B, B authorises C — how the caps compose, and who carries the loss when they do not.  
* Enforcing it, and the moment it stops  
  * A purchase refused because the merchant or the category falls outside the mandate.  
  * A checkout stopped mid-way, when shipping and tax push the total past the cap.  
  * A stop that triggers on velocity rather than amount: N purchases inside M minutes.  
  * A revocation that arrives while the agent is already mid-transaction.  
  * An escalation, where the agent decides it cannot decide, asks, and the request expires unanswered.  
* Showing what happened  
  * A log a third party can check without having to trust the operator.  
  * Answering "why did it do that" from the recorded rule, rather than from an explanation written afterwards.  
  * A discrepancy between what the agent reported and what the statement eventually shows.  
  * Disputing a purchase that should not have happened — reversal, reconciliation, and who ends up holding the loss.  
  * A record a merchant will accept, and a merchant that will not.

* Paying, and what a reward is worth  
  * Choosing a route when fee, timing and currency all matter.  
  * What a loyalty point is actually worth, and the point at which that changes the ranking.  
  * A reversal that must also unwind the reward earned on the original purchase.  
  * The same basket cost five ways, with the spread shown.  
* Adversarial, and measuring the failure  
  * A listing written to manipulate the agent rather than inform the buyer.  
  * Instructions injected through a product description, a photograph or a review.  
  * A seller that detects an agent and prices it differently.  
  * Agent-to-agent negotiation over a machine-readable catalogue, including the case where it fails.  
  * A test harness that reports how often an agent overspends, over a replayed set of scenarios.

**EVIDENCE**  
Show the log of what the agent was permitted to do, then show it being stopped — a limit that was held, a mandate that expired, a spend that was refused. Compare against the manual route: the steps a person takes today, the time it costs them, and what it costs them. State the rule by which the agent decides, so a user could understand why it did what it did. Every rate, fee, cashback percentage or points value must be one you observed and timestamped; anything else is a fabrication, and will be treated as one.

# 

# 

# 

# 

# 

2. ### **Financial Technology Problem Statement: Recognise Value That Gets Overlooked**

**CHALLENGE**  
Useful contributions and resources are not always easy to recognise, coordinate, or exchange. Time, equipment, space, skills and credit sit idle next to the people who need them, because no process exists to see them or to settle the terms. Build something that supports a financial or economic activity around value that existing processes handle poorly.

**SCOPE REQUIREMENT**  
Define what is being contributed or exchanged, who benefits, and one rule that keeps the arrangement fair. Demonstrate a disagreement, a withdrawal, or an imbalance, and what your product does about it. A new currency, token or blockchain is not required.

**POTENTIAL DIRECTIONS**

* Time, and what an hour is worth  
  * A community time exchange where an hour of plumbing equals an hour of tutoring — and the first case where that equivalence breaks down.  
  * A rota of care, where the people who owe hours and the people who are owed them are the same people at different times.  
  * A skill swap inside one small group, where nobody can invoice anybody and the credit still has to be tracked.  
  * One hour of work valued differently by the person giving it and the person receiving it.  
* Things already owned, sitting idle  
  * A shared tool library where the deposit, not the loan, is the hard part.  
  * Equipment that is idle most of the week, and the one rule that decides who gets it next.  
  * A kitchen, a van or a room the group already has, with a calendar that prices priority rather than just booking.  
  * A seat on a subscription that could be shared without breaking its terms.  
* Buying together  
  * Collective purchasing, where one member's withdrawal changes the price for everyone who stays.  
  * A group order where one person fronted the money, and the settlement afterwards is the whole problem.  
  * A buying club whose rule decides who loses when the order only partly fills.  
  * A cooperative that has to survive a member leaving mid-cycle.  
* Cost sharing, and a rule that keeps it fair  
  * Splitting a shared bill where usage is unequal and measurable.  
  * A contribution model where a member's capacity to pay is itself one of the variables.  
  * Charging by ability rather than by usage, and the resentment that produces.  
  * A rule that protects the weakest member and costs the heaviest user, with both of them in the room.

* Credit and trust between people  
  * Informal credit between neighbours or small businesses, where the ledger today is somebody's word.  
  * A barter between two small businesses, where each side has to price its own work.  
  * A record of past dealings that makes an informal loan possible without a bank.  
  * The case where one side says the work was not done.  
* Making it visible, and where it breaks  
  * The value of unpaid work that never reaches a ledger, measured, so that it can at least be argued about.  
  * A contribution that is easy to see but hard to price: a referral, a reputation, a favour recalled years later.  
  * A periodic statement for a household or a group, covering work that currently has no number at all.  
  * Someone games the fairness rule, and the rule has to be revised in public.  
  * A member withdraws, and everything they contributed has to be unwound.  
  * The unit of account drifts: what one credit buys changes as participation changes.

**EVIDENCE**  
Show the allocation or exchange process running consistently across more than one case, and explain the trade-off your fairness rule creates — who it protects, and who it costs. State where the arrangement breaks.

# 

# 

# 

# 

3. ### **Deep Technology Problem Statement: Test the Change Before You Make It**

**CHALLENGE**  
Changing a physical system is expensive to get wrong in the world, and cheap to get wrong on paper. Build something that lets someone explore a change before committing money, space or materials, by turning a scientific or engineering relationship into an experiment or a decision tool they can actually use.

**SCOPE REQUIREMENT**  
Model one small system, with at least two adjustable inputs and one practical constraint. Demonstrate a trade-off between two outcomes — the system must not be able to improve both at once. You choose the system, the inputs, the outputs and the interface.

**POTENTIAL DIRECTIONS**

* Small physical systems, with the trade-off named  
  * Room shading and daylight — shade depth and angle against window area. More shade cools the room and darkens it, and the constraint is the view you still want.  
  * Water storage and flow — tank volume against outlet size. More storage buys a longer dry spell and costs weight, space and headroom, on a roof or a floor that carries only so much.  
  * Packaging geometry and material use — box dimensions against board grade. More protection costs material, weight and freight volume, inside a fixed shipping tier.  
  * Workstation or furniture layout — desk spacing against seat depth. Fitting more people in means less clearance, in a room whose walls do not move.  
  * A growing space — light hours against planting density. Under a fixed light or power budget, more plants means less light each, and total yield can fall as planted area rises.  
  * Heat and ventilation in a small room — vent area against fan rate. More airflow cools the room and carries dust; a filter that cleans the air also chokes the flow.  
  * A structure carrying a load — member depth against span. Strength and stiffness against material weight and cost, using only the stock sizes you can actually buy.  
* The same relationship, for a decision someone makes at home  
  * Which air-conditioner: capacity against running cost and noise.  
  * Where the fridge goes: usable size against clearance and energy.  
  * Whether to insulate: cost now against comfort and bills later.  
  * Curtains, blinds or film: light, heat and privacy, and you cannot have all three.  
  * A home workspace: what the desk needs against what the room can give.  
  * A water filter: filtration grade against flow rate and how often you replace the cartridge.

* Systems worth adding to the list  
  * Hot water for a small flat: tank volume against heater power — a longer shower against the space, the weight and the standing loss the tank costs.  
  * A battery or backup supply: capacity against load — more hours of backup against cost, charge time, and the weight somebody has to carry.  
  * Noise and airflow through the same opening: absorption area against free area — quiet against ventilated.  
  * Drying without a dryer: airflow and stacking density against time — a bigger load against more time and more floor.  
  * Rainwater or greywater for a roof or a garden: catchment area against store size, against what the structure will take and what happens in a downpour.  
* Checking it against something real  
  * Build the bench as well as the model: one measured case that the model has to reproduce, and what you do when it does not.  
  * A model whose inputs are things you can measure with a phone, a tape and a thermometer, and nothing else.  
  * Two configurations run side by side, with the value you would choose and the reason stated.

**EVIDENCE**  
Check the model against a known relationship or a reference case, and show the check. Then compare at least two alternative configurations and say which you would choose, and why. Label anything simulated, and state which physical effects your model leaves out.

# 

# 

4. ### **Deep Technology Problem Statement: The Capability That Hasn't Travelled**

**CHALLENGE**  
Some capabilities work beautifully in a laboratory, in an instrument, or in an expert's hands, and are useless in the place where they would matter most, because that place cannot afford the instrument, has no signal, has no room, or has nobody who can read the output. Pick one such capability — a model, an agent, a sensing method, or a verification method. Move it, intact, into a setting where it is currently out of reach. Name the barrier that keeps it out, and build something that works anyway.

**SCOPE REQUIREMENT**  
Identify the capability, the new setting, and one barrier to adoption — cost, connectivity, space, or user expertise. Demonstrate one complete task under that barrier, on something the person next to you can pick up and drive. You choose the user, the data and the method.

**POTENTIAL DIRECTIONS**

* Sensing on the device, with nothing else in the room  
  * Speech recognised, and speech translated, with no connectivity at all.  
  * Reading a printed form, a label, a meter or a prescription in bad light or at an angle.  
  * Identifying a plant, a pest, a part or a fault from a photograph taken by someone who does not know what they are looking at.  
  * Turning a phone or a laptop microphone into a usable sound or vibration instrument.  
  * Measuring level, angle, distance or colour with the sensors that are already in the room.  
* From a signal to a diagnosis  
  * A pump, a fan or a motor heard failing before it fails.  
  * A leak or a blockage located by sound, inside a wall or under a floor.  
  * A machine health check that a non-technical owner can run every week.  
  * An image turned into an instruction rather than a label: not "corrosion" but "do this next".  
* Deciding without a specialist  
  * A solver for a rota, a route or a room layout, where the scheduler today is a person with a spreadsheet.  
  * A constraint solver that a manager can adjust and re-run without understanding how it works.  
  * A schedule that reports why it chose what it chose, so that it can be argued with.  
  * An optimisation that has to run on a phone, in a shop, inside a minute.  
* Proving something without a gatekeeper  
  * A verifiable record for a claim somebody currently has to take on trust: that a check was done, that a photograph is unedited, that a document has not changed.  
  * A tamper-evident log for a small organisation that has no IT department.  
  * An attestation a counterparty can check without an account, a fee or a signature.  
    A receipt for work that is currently agreed by memory.

* Running unattended  
  * An agent that completes a repetitive or bureaucratic task end to end, and reports honestly what it could not do.  
  * A data entry flow that handles the exception instead of stopping on it.  
    — A first-line response that escalates rather than guesses.  
    — Something that runs overnight, and can be checked in the morning.  
* Where it lands  
  * A small shop, a clinic, a workshop, a studio, a school, a community group, a household.  
  * A task someone currently does weekly, by hand, with no budget for software.  
  * A record-keeping job that only exists because nobody has digitised it.  
  * A judgement call that is currently made by whoever happens to be in the room.

**EVIDENCE**  
Compare against the setting's current manual method, or a simple substitute — the steps and the time — and show what the capability contributes that the manual method cannot. State what it costs, what it gets wrong, and what leaves the device.

[image1]: <data:image/png;base64,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>