import { configure } from "@testing-library/react";

// Route chunks load lazily and transform on first use, which a cold CI runner can take over a second to do.
configure({ asyncUtilTimeout: 5000 });
