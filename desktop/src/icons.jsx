// Placeholder mark (planet, orbit, wings) and line icons. Swap Logo for the real artwork later.
export function Logo({ size = 34 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-label="Mercury">
      <defs>
        <linearGradient id="mlogo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="var(--accent2)" />
          <stop offset=".5" stopColor="var(--accent)" />
          <stop offset="1" stopColor="var(--accent2)" />
        </linearGradient>
      </defs>
      <ellipse cx="32" cy="32" rx="29" ry="11" fill="none" stroke="url(#mlogo)" strokeWidth="1.6" opacity=".55"
               transform="rotate(-24 32 32)" />
      <g className="orbit"><circle cx="58" cy="25" r="2.6" fill="var(--accent)" /></g>
      <circle cx="32" cy="32" r="13" fill="url(#mlogo)" />
      <path d="M19 26c-6-1-10-5-12-10 6 2 10 2 14 5M45 26c6-1 10-5 12-10-6 2-10 2-14 5" fill="none"
            stroke="url(#mlogo)" strokeWidth="2.2" strokeLinecap="round" />
      <circle cx="27" cy="28" r="3.2" fill="#fff" opacity=".45" />
    </svg>
  );
}

const paths = {
  chat: <path d="M4 5h16v11H9l-5 4z" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />,
  devices: (
    <>
      <rect x="3" y="4" width="14" height="10" rx="2" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <path d="M1 18h18" stroke="currentColor" strokeWidth="1.7" />
      <rect x="18" y="8" width="5" height="12" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.7" />
    </>
  ),
  tasks: <path d="M5 12l4 4L19 6" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />,
  skills: (
    <path d="M12 3l2.6 5.6L20 9.5l-4 4 1 5.8-5-2.8-5 2.8 1-5.8-4-4 5.4-.9z" fill="none" stroke="currentColor"
          strokeWidth="1.6" strokeLinejoin="round" />
  ),
  connections: (
    <>
      <circle cx="12" cy="12" r="3.2" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"
            stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </>
  ),
  shield: <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z" fill="none" stroke="currentColor" strokeWidth="1.7" />,
  windows: <path d="M3 5l8-1v7H3zM13 4l8-1v8h-8zM3 13h8v7l-8-1zM13 13h8v8l-8-1z" fill="currentColor" />,
  android: (
    <>
      <rect x="6" y="2" width="12" height="20" rx="3" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <path d="M10 18h4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </>
  ),
  linux: (
    <>
      <rect x="3" y="4" width="18" height="13" rx="2" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <path d="M7 9l3 2-3 2M12 13h4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" fill="none" />
    </>
  ),
};

export function Icon({ name }) {
  return <svg viewBox="0 0 24 24">{paths[name] || paths.linux}</svg>;
}

export const platformIcon = (platform) =>
  ({ windows: "windows", android: "android" })[platform] || "linux";
