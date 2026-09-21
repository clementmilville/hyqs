import reactPlugin from "eslint-plugin-react";

export default [
  // Generated, published static assets are build output, not source (see .prettierignore).
  { ignores: ["public/**", "dist/**"] },
  {
    files: ["**/*.js", "**/*.jsx"],
    plugins: { react: reactPlugin },
    languageOptions: {
      globals: {
        window: "readonly",
        document: "readonly",
        console: "readonly",
        fetch: "readonly",
        React: "readonly",
      },
      parserOptions: {
        ecmaFeatures: {
          jsx: true,
        },
      },
    },
    rules: {
      "react/jsx-uses-vars": "error",
      "no-unused-vars": ["error", { caughtErrorsIgnorePattern: "^_" }],
    },
  },
];
