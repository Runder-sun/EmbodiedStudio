"""Current contact/joint signals and resettable one-shot/edge/continuous rules."""
from copy import deepcopy


class Interactions:
    def __init__(self, rules):
        self.rules = rules
        self.reset()

    def reset(self):
        self.state, self.previous, self.fired, self.events = {}, {}, set(), []

    def update(self, time, current_contacts, joints):
        effects = []
        for rule in self.rules:
            active = (rule['target'] in current_contacts if rule['event'] == 'robot_contact'
                      else joints.get(rule['target'], float('-inf')) >= rule['threshold'])
            mode = rule.get('mode', 'once')
            previous = self.previous.get(rule['id'], False)
            fire = active and (mode == 'continuous' or (mode == 'edge' and not previous)
                               or (mode == 'once' and rule['id'] not in self.fired))
            self.previous[rule['id']] = active
            if not fire:
                continue
            self.fired.add(rule['id'])
            effect = deepcopy(rule['effect'])
            self.state.setdefault(effect['target'], {}).update(effect.get('state', {}))
            self.events.append({'time': time, 'interactionId': rule['id'], 'source': rule['target'],
                                'event': rule['event'], 'effect': effect})
            effects.append(effect)
        return effects
