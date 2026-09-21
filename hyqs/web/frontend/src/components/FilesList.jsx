export function FilesList({ files }) {
  if (!files?.length) return null;
  return (
    <ul className="files">
      {files.map((f, i) => (
        <li key={i}>
          <code>{f.path}</code> <span className="add">+{f.added}</span>{" "}
          <span className="del">−{f.deleted}</span>
        </li>
      ))}
    </ul>
  );
}
