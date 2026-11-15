// LinkedIn's reactions under the names LinkedIn shows (#320). An activity keeps the
// API's name in details.reaction; a Like is "liked", any other "reacted".

const REACTIONS = {
  LIKE: ['👍', 'Like'],
  PRAISE: ['👏', 'Celebrate'],
  APPRECIATION: ['🫶', 'Support'],
  EMPATHY: ['❤️', 'Love'],
  INTEREST: ['💡', 'Insightful'],
  ENTERTAINMENT: ['😂', 'Funny'],
};

// "😂 Funny", or '' when the activity has no reaction
export function reactionText(details) {
  const name = details && details.reaction;
  if (!name) return '';
  const [emoji, label] = REACTIONS[name] || ['', name.charAt(0) + name.slice(1).toLowerCase()];
  return emoji ? `${emoji} ${label}` : label;
}
