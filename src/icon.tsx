// Original Clean Mic icon: a microphone capsule with a sound wave being cut clean.
export function CleanMicIcon({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="8" y="2.5" width="8" height="12" rx="4" />
      <path d="M5 11.5a7 7 0 0 0 14 0" />
      <path d="M12 18.5v3" />
      <path d="M8.5 21.5h7" />
      <path d="M1.5 6.5l1.5 -1.5l1.5 1.5l1.5 -1.5" strokeWidth={1.6} opacity={0.55} />
      <path d="M18.5 6h4" strokeWidth={1.6} />
    </svg>
  );
}
